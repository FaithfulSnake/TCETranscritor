#!/usr/bin/env bash
# Instalador do TCE Transcritor.
#
# No Termux (Android), cole e tecle Enter:
#   curl -fsSL https://raw.githubusercontent.com/FaithfulSnake/TCETranscritor/HEAD/instalar.sh | bash
#
# Pode rodar de novo quando quiser: ele só refaz o que faltar.
# Variáveis opcionais:
#   TCE_MODELOS="small turbo"   modelos a baixar (base, small, medium, turbo, large)
#   TCE_COMPATIVEL=1            compila o whisper.cpp sem otimizações específicas do processador
#   TCE_SO_WHISPER=1            só (re)compila o whisper.cpp
#   TCE_FONTE=/pasta            usa os scripts desta pasta em vez de baixar do GitHub (testes)
#   TCE_TESTE=1                 modo de teste automático (sem perguntas, sem permissão de armazenamento)
set -eo pipefail

REPO_URL="${TCE_REPO_URL:-https://github.com/FaithfulSnake/TCETranscritor}"
WHISPER_VERSAO="${TCE_WHISPER_VERSAO:-v1.9.5}"
TCE_DADOS="${TCE_DADOS:-$HOME/.local/share/tce}"
TCE_APP="$TCE_DADOS/app"
MODELOS_PEDIDOS="${TCE_MODELOS:-small turbo}"
CONFIG="$HOME/.config/tce/config"

cor()   { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
passo() { echo; cor "1;36" "==> $*"; }
ok()    { cor "32" "    ok: $*"; }
aviso() { cor "33" "    atenção: $*"; }
morrer(){ cor "31" "ERRO: $*"; exit 1; }

eh_termux() { [ -n "${PREFIX:-}" ] && [[ "$PREFIX" == *com.termux* ]]; }

if eh_termux; then
  BIN_GLOBAL="$PREFIX/bin"
else
  BIN_GLOBAL="$HOME/.local/bin"
fi
mkdir -p "$TCE_DADOS/bin" "$TCE_DADOS/modelos" "$BIN_GLOBAL" "$HOME/.config/tce"

nucleos() { nproc 2>/dev/null || echo 4; }
jobs_compilacao() {   # menos processos em paralelo em celular com pouca memória
  local mem_kb n
  mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 4000000)
  n=$(nucleos)
  if [ "$mem_kb" -lt 3500000 ]; then echo 2
  elif [ "$mem_kb" -lt 6000000 ]; then echo $(( n < 4 ? n : 4 ))
  else echo $(( n < 6 ? n : 6 )); fi
}

# ---------------------------------------------------------------------------
instalar_pacotes() {
  passo "Pacotes do sistema (pode levar alguns minutos)"
  if eh_termux; then
    export DEBIAN_FRONTEND=noninteractive
    local opts=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
    pkg update -y >/dev/null 2>&1 || apt-get update || true
    apt-get "${opts[@]}" upgrade || aviso "não consegui atualizar todos os pacotes (seguindo mesmo assim)"
    apt-get "${opts[@]}" install python python-pip ffmpeg git cmake clang make curl \
      || morrer "falha ao instalar os pacotes básicos. Rode 'pkg update' e tente de novo."
    # opcionais: cada um separado para um nome ausente não derrubar a instalação
    local p
    for p in deno python-numpy python-brotli python-pycryptodomex termux-api; do
      apt-get "${opts[@]}" install "$p" >/dev/null 2>&1 && ok "$p" || aviso "pacote $p não disponível (opcional)"
    done
    if ! command -v deno >/dev/null 2>&1; then
      apt-get "${opts[@]}" install nodejs >/dev/null 2>&1 && ok "nodejs (no lugar do deno)" || true
    fi
  else
    local falta=()
    for p in python3 ffmpeg git cmake curl; do command -v "$p" >/dev/null 2>&1 || falta+=("$p"); done
    command -v c++ >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1 || falta+=("g++")
    if [ ${#falta[@]} -gt 0 ]; then
      if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
        sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-venv ffmpeg git cmake g++ curl
      else
        morrer "instale antes: ${falta[*]}"
      fi
    fi
  fi
  ok "pacotes"
}

instalar_ytdlp() {
  passo "yt-dlp (pega o áudio direto do YouTube)"
  if eh_termux; then
    python -m pip install -U --quiet "yt-dlp[default]" 2>&1 | tail -3 \
      || python -m pip install -U --quiet yt-dlp yt-dlp-ejs \
      || morrer "falha ao instalar o yt-dlp"
  else
    if [ ! -x "$TCE_DADOS/venv/bin/python" ]; then
      python3 -m venv "$TCE_DADOS/venv" || morrer "falha ao criar o ambiente Python (instale python3-venv)"
    fi
    "$TCE_DADOS/venv/bin/python" -m pip install -U --quiet "yt-dlp[default]" || morrer "falha ao instalar o yt-dlp"
    "$TCE_DADOS/venv/bin/python" -m pip install --quiet numpy >/dev/null 2>&1 || true
  fi
  # o YouTube exige um "motor" de JavaScript: deno (padrão) ou node
  if ! command -v deno >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/yt-dlp"
    grep -qs -- "--js-runtimes" "$HOME/.config/yt-dlp/config" \
      || echo "--js-runtimes node" >> "$HOME/.config/yt-dlp/config"
    ok "yt-dlp vai usar o node"
  fi
  command -v deno >/dev/null 2>&1 || command -v node >/dev/null 2>&1 \
    || aviso "sem deno/node: o YouTube pode recusar alguns downloads"
  ok "yt-dlp $( (yt-dlp --version || "$TCE_DADOS/venv/bin/yt-dlp" --version) 2>/dev/null | head -1)"
}

instalar_scripts() {
  passo "Scripts do TCE Transcritor"
  if [ -n "${TCE_FONTE:-}" ]; then
    rm -rf "$TCE_APP"
    mkdir -p "$TCE_APP"
    cp -R "$TCE_FONTE"/. "$TCE_APP"/
  elif [ -d "$TCE_APP/.git" ]; then
    git -C "$TCE_APP" pull --ff-only --quiet || aviso "não consegui atualizar (seguindo com a versão atual)"
  else
    rm -rf "$TCE_APP"
    git clone --depth 1 --quiet "$REPO_URL" "$TCE_APP" || morrer "não consegui baixar os scripts de $REPO_URL"
  fi
  chmod +x "$TCE_APP/tce/tce" "$TCE_APP/tce/motor.py" 2>/dev/null || true
  ok "$TCE_APP"
}

compilar_whisper() {
  local src="$TCE_DADOS/whisper.cpp"
  if [ ! -f "$src/CMakeLists.txt" ] || [ "$(cat "$src/.versao_tce" 2>/dev/null)" != "$WHISPER_VERSAO" ]; then
    passo "Baixando o whisper.cpp $WHISPER_VERSAO"
    rm -rf "$src"
    git clone --depth 1 --quiet --branch "$WHISPER_VERSAO" https://github.com/ggml-org/whisper.cpp "$src" \
      || morrer "não consegui baixar o whisper.cpp"
    echo "$WHISPER_VERSAO" > "$src/.versao_tce"
  fi
  local flags=(-DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF
               -DWHISPER_SDL2=OFF -DGGML_OPENMP=OFF)
  if [ -n "${TCE_COMPATIVEL:-}" ]; then
    flags+=(-DGGML_NATIVE=OFF)
    aviso "modo compatível: mais lento, mas roda em qualquer processador"
  fi
  local j; j=$(jobs_compilacao)
  passo "Compilando o whisper.cpp ($j processos; leva de 5 a 20 minutos no celular)"
  rm -rf "$src/build"
  cmake -S "$src" -B "$src/build" "${flags[@]}" >"$TCE_DADOS/compilacao.log" 2>&1 \
    || { tail -30 "$TCE_DADOS/compilacao.log"; morrer "falha ao configurar a compilação"; }
  cmake --build "$src/build" -j "$j" --target whisper-cli >>"$TCE_DADOS/compilacao.log" 2>&1 \
    || { tail -30 "$TCE_DADOS/compilacao.log"; morrer "falha ao compilar o whisper.cpp (veja $TCE_DADOS/compilacao.log)"; }
  install -m 755 "$src/build/bin/whisper-cli" "$TCE_DADOS/bin/whisper-cli"
  ok "whisper-cli compilado"
}

instalar_comandos() {
  passo "Comandos e atalhos"
  local bash_bin; bash_bin="$(command -v bash)"
  cat > "$BIN_GLOBAL/tce" <<EOF
#!$bash_bin
exec "$bash_bin" "$TCE_APP/tce/tce" "\$@"
EOF
  chmod +x "$BIN_GLOBAL/tce"
  ok "comando 'tce'"

  if eh_termux; then
    mkdir -p "$HOME/bin" "$HOME/.shortcuts"
    local f
    for f in termux-url-opener termux-file-editor; do
      if [ -f "$HOME/bin/$f" ] && ! grep -q "TCE Transcritor" "$HOME/bin/$f"; then
        mv "$HOME/bin/$f" "$HOME/bin/$f.antes-do-tce"
        aviso "o seu $f antigo foi guardado como $f.antes-do-tce"
      fi
    done
    cat > "$HOME/bin/termux-url-opener" <<EOF
#!$bash_bin
# TCE Transcritor: YouTube > Compartilhar > Termux
exec "$bash_bin" "$TCE_APP/tce/tce" link "\$1"
EOF
    cat > "$HOME/bin/termux-file-editor" <<EOF
#!$bash_bin
# TCE Transcritor: arquivo de áudio > Compartilhar > Termux
exec "$bash_bin" "$TCE_APP/tce/tce" compartilhado "\$1"
EOF
    cat > "$HOME/.shortcuts/TCE Transcritor" <<EOF
#!$bash_bin
exec "$bash_bin" "$TCE_APP/tce/tce" menu
EOF
    chmod +x "$HOME/bin/termux-url-opener" "$HOME/bin/termux-file-editor" "$HOME/.shortcuts/TCE Transcritor"
    ok "Compartilhar > Termux (links do YouTube e arquivos de áudio)"
    ok "atalho para a tela inicial (app Termux:Widget, opcional)"
  fi

  if [ ! -f "$CONFIG" ]; then
    cat > "$CONFIG" <<'EOF'
# Configuração do TCE Transcritor (apague o # do começo da linha para mudar)

# Pasta dos TXT (padrão: Downloads/TCE-Transcricoes)
#PASTA_SAIDA="$HOME/storage/downloads/TCE-Transcricoes"

# Modelo da transcrição AO VIVO (precisa ser rápido): base, small
MODELO_AOVIVO="small"

# Modelo da versão FIEL (integral): turbo (recomendado), medium, large
MODELO_FIEL="turbo"

# Quantos núcleos do processador usar (vazio = automático)
#THREADS=4

# Quando a live acabar: perguntar | celular | nuvem | nao
FIEL_DEPOIS_DA_LIVE="perguntar"

# Guardar o áudio da live (m4a) junto do TXT depois da versão fiel (1 = sim)
GUARDAR_AUDIO=1

# Notificação com o progresso (precisa do app Termux:API instalado): 1 = sim
NOTIFICAR=0

# Nuvem (GitHub Actions)
#REPO="FaithfulSnake/TCETranscritor"
MODELO_NUVEM="turbo"
PARTES_NUVEM=8

# Palavras que ajudam o modelo a acertar nomes e termos (opcional)
#PROMPT="Sessão do Tribunal de Contas do Estado do Maranhão. Conselheiro Fulano, conselheira Beltrana."
EOF
    ok "configuração em $CONFIG"
  fi
}

baixar_modelos() {
  passo "Modelos de transcrição (use Wi-Fi: ~850 MB no total)"
  local m
  for m in $MODELOS_PEDIDOS; do
    "$BIN_GLOBAL/tce" modelos baixar "$m" || morrer "não consegui baixar o modelo $m"
  done
}

testar() {
  passo "Teste do whisper.cpp"
  local amostra="$TCE_DADOS/whisper.cpp/samples/jfk.wav" modelo="" m
  for m in ggml-base-q8_0.bin ggml-small-q8_0.bin ggml-tiny-q8_0.bin ggml-large-v3-turbo-q5_0.bin; do
    [ -s "$TCE_DADOS/modelos/$m" ] && { modelo="$TCE_DADOS/modelos/$m"; break; }
  done
  [ -n "$modelo" ] || { aviso "nenhum modelo para testar"; return 0; }
  local saida rc=0
  saida="$("$TCE_DADOS/bin/whisper-cli" -m "$modelo" -f "$amostra" -l en -nt -np -t "$(jobs_compilacao)" 2>&1)" || rc=$?
  if [ "$rc" -eq 132 ] || [ "$rc" -eq 4 ] || echo "$saida" | grep -qi "illegal instruction"; then
    if [ -z "${TCE_COMPATIVEL:-}" ]; then
      aviso "o processador recusou as otimizações; recompilando no modo compatível"
      TCE_COMPATIVEL=1 compilar_whisper
      testar
      return
    fi
    morrer "o whisper.cpp não roda neste aparelho (instrução ilegal)"
  fi
  if echo "$saida" | grep -qi "country"; then
    ok "o whisper.cpp transcreveu a amostra: $(echo "$saida" | tr -s ' \n' ' ' | cut -c1-80)"
  else
    echo "$saida" | tail -15
    morrer "o teste do whisper.cpp falhou"
  fi
}

escolher_modelo_aovivo() {
  [ -s "$TCE_DADOS/modelos/ggml-small-q8_0.bin" ] || return 0
  passo "Medindo a velocidade deste aparelho (uns 30 segundos)"
  local res fator
  res="$("$BIN_GLOBAL/tce" velocidade 2>&1 || true)"
  echo "$res" | grep -E "tempo real" || true
  fator="$(echo "$res" | sed -nE 's/^ *small +([0-9.]+)x.*/\1/p' | head -1)"
  if [ -n "$fator" ] && awk "BEGIN {exit !($fator < 1.6)}"; then
    aviso "o modelo small é lento demais para ao vivo neste aparelho; usando o base"
    "$BIN_GLOBAL/tce" modelos baixar base || true
    sed -i 's/^MODELO_AOVIVO=.*/MODELO_AOVIVO="base"/' "$CONFIG"
  fi
}

pedir_armazenamento() {
  eh_termux || return 0
  [ -n "${TCE_TESTE:-}" ] && return 0
  [ -d "$HOME/storage/downloads" ] && return 0
  passo "Permissão para salvar os TXT na pasta Download do celular"
  echo "    Vai aparecer um aviso do Android: toque em PERMITIR."
  termux-setup-storage || true
  local _
  for _ in $(seq 1 60); do [ -d "$HOME/storage/downloads" ] && break; sleep 1; done
  [ -d "$HOME/storage/downloads" ] && ok "pasta Download liberada" \
    || aviso "sem a permissão os TXT ficam em ~/TCE-Transcricoes (rode 'termux-setup-storage' depois)"
}

# ---------------------------------------------------------------------------
cor "1;35" "TCE Transcritor - instalação"
if [ -n "${TCE_SO_WHISPER:-}" ]; then
  compilar_whisper
  testar
  exit 0
fi
pedir_armazenamento
instalar_pacotes
instalar_ytdlp
instalar_scripts
compilar_whisper
instalar_comandos
baixar_modelos
testar
escolher_modelo_aovivo

echo
cor "1;32" "Tudo pronto!"
cat <<EOF

Como usar:
  * No YouTube, abra a live ou o vídeo > Compartilhar > Termux > escolha a opção.
  * Ou digite:  tce          (menu)
                tce aovivo LINK
                tce gravada LINK
                tce ajuda
  * Os TXT ficam na pasta Download/TCE-Transcricoes do celular.
  * Para a nuvem do GitHub (rápida e grátis):  tce nuvem-configurar
EOF
