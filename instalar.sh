#!/usr/bin/env bash
# Instalador do TCE Transcritor.
#
# No Termux (Android) ou no Terminal do Mac, cole e tecle Enter:
#   curl -fsSL https://raw.githubusercontent.com/FaithfulSnake/TCETranscritor/HEAD/instalar.sh | bash
# (No Mac não precisa de senha de administrador, Xcode nem Homebrew.)
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
eh_mac()    { [ "$(uname -s)" = "Darwin" ]; }
mac_arm()   { [ "$(uname -m)" = arm64 ] || [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; }  # chip Apple (até sob Rosetta)
URL_BIN_MAC="$REPO_URL/releases/download/binarios-mac"
VERSAO_BIN_MAC="whisper-$WHISPER_VERSAO+ffmpeg-8.0"   # mude junto com o binarios-mac.yml

if eh_termux; then
  BIN_GLOBAL="$PREFIX/bin"
else
  BIN_GLOBAL="$HOME/.local/bin"
fi
mkdir -p "$TCE_DADOS/bin" "$TCE_DADOS/modelos" "$BIN_GLOBAL" "$HOME/.config/tce"

nucleos() { nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4; }
jobs_compilacao() {   # menos processos em paralelo em celular com pouca memória
  local mem_kb n
  mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 4000000)
  n=$(nucleos)
  if [ "$mem_kb" -lt 3500000 ]; then echo 2
  elif [ "$mem_kb" -lt 6000000 ]; then echo $(( n < 4 ? n : 4 ))
  else echo $(( n < 6 ? n : 6 )); fi
}

# ---------------------------------------------------------------------------
# Servidores de pacotes do Termux. O `pkg` sorteia um espelho e só confere se ele
# responde; um espelho no meio da sincronização ("Mirror sync in progress?") passa
# nessa conferência e entrega um índice quebrado. Aqui o índice é conferido de
# verdade e, se falhar, o instalador troca para o servidor oficial ou outro espelho.
PACOTES_BASE=(python python-pip ffmpeg git cmake clang make curl)

indice_ok() {
  local saida rc=0
  saida="$(apt-get update 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ] || echo "$saida" | grep -qE "^(E|Err):|Failed to fetch|Mirror sync|Hash Sum mismatch|unexpected size"; then
    echo "$saida" | grep -E "^(E|Err):" | head -3 | sed 's/^/    /'
    return 1
  fi
  apt-cache show "${PACOTES_BASE[@]}" >/dev/null 2>&1
}

usar_espelho() {  # usar_espelho ARQUIVO  (arquivos em $PREFIX/etc/termux/mirrors)
  local arq="$1" dir="$PREFIX/etc/apt"
  [ -f "$arq" ] || return 1
  (
    unset MAIN X11 ROOT WEIGHT
    # shellcheck source=/dev/null
    . "$arq"
    [ -n "${MAIN:-}" ] || exit 1
    if [ -f "$dir/sources.list.d/main.sources" ]; then
      sed -i "s|^URIs:.*|URIs: $MAIN|" "$dir/sources.list.d/main.sources"
    else
      echo "deb $MAIN stable main" > "$dir/sources.list"
    fi
    if [ -n "${X11:-}" ]; then
      [ -f "$dir/sources.list.d/x11.sources" ] && sed -i "s|^URIs:.*|URIs: $X11|" "$dir/sources.list.d/x11.sources"
      [ -f "$dir/sources.list.d/x11.list" ] && echo "deb $X11 x11 main" > "$dir/sources.list.d/x11.list"
    fi
    if [ -n "${ROOT:-}" ]; then
      [ -f "$dir/sources.list.d/root.sources" ] && sed -i "s|^URIs:.*|URIs: $ROOT|" "$dir/sources.list.d/root.sources"
      [ -f "$dir/sources.list.d/root.list" ] && echo "deb $ROOT root stable" > "$dir/sources.list.d/root.list"
    fi
    exit 0
  )
}

preparar_indice() {
  local espelhos="$PREFIX/etc/termux/mirrors" arq
  echo "    conferindo o servidor de pacotes..."
  if indice_ok; then ok "servidor de pacotes"; return 0; fi
  aviso "o servidor de pacotes do Termux usado por este celular está com problema; trocando para o oficial"
  if usar_espelho "$espelhos/default" && indice_ok; then ok "usando o servidor oficial do Termux"; return 0; fi
  while IFS= read -r arq; do
    echo "    tentando outro servidor: $(basename "$arq")"
    if usar_espelho "$arq" && indice_ok; then ok "usando $(basename "$arq")"; return 0; fi
  done < <(find "$espelhos" -type f ! -name default ! -name '*.dpkg-*' ! -name '*~' 2>/dev/null | shuf | head -6)
  return 1
}

instalar_pacotes() {
  passo "Pacotes do sistema (pode levar alguns minutos)"
  if eh_termux; then
    export DEBIAN_FRONTEND=noninteractive
    export TERMUX_PKG_NO_MIRROR_SELECT=1   # o `pkg` não sorteia de novo durante a instalação
    local opts=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
    preparar_indice || morrer "nenhum servidor de pacotes do Termux respondeu direito. Confira a internet e rode o instalador de novo daqui a alguns minutos (ou rode 'termux-change-repo', escolha outro servidor e tente de novo)."
    apt-get "${opts[@]}" upgrade || aviso "não consegui atualizar todos os pacotes (seguindo mesmo assim)"
    apt-get "${opts[@]}" install "${PACOTES_BASE[@]}" \
      || morrer "falha ao instalar os pacotes básicos. Rode o instalador de novo; se repetir, rode 'termux-change-repo', escolha outro servidor e tente de novo."
    # opcionais: cada um separado para um nome ausente não derrubar a instalação
    local p
    for p in deno python-numpy python-brotli python-pycryptodomex termux-api; do
      apt-get "${opts[@]}" install "$p" >/dev/null 2>&1 && ok "$p" || aviso "pacote $p não disponível (opcional)"
    done
    if ! command -v deno >/dev/null 2>&1; then
      apt-get "${opts[@]}" install nodejs >/dev/null 2>&1 && ok "nodejs (no lugar do deno)" || true
    fi
  elif eh_mac; then
    local p
    for p in curl unzip tar; do command -v "$p" >/dev/null 2>&1 || morrer "falta o comando $p no Mac"; done
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
  elif eh_mac; then
    local uv; uv="$(command -v uv || echo "$TCE_DADOS/bin/uv")"
    if [ ! -x "$uv" ]; then
      curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR="$TCE_DADOS/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null \
        || morrer "não consegui instalar o uv (que traz o Python)"
      uv="$TCE_DADOS/bin/uv"
    fi
    if ! "$TCE_DADOS/venv/bin/python" -c "" >/dev/null 2>&1; then
      rm -rf "$TCE_DADOS/venv"
      "$uv" venv --quiet --python 3.13 --python-preference only-managed "$TCE_DADOS/venv" \
        || morrer "falha ao preparar o Python"
    fi
    ok "Python $("$TCE_DADOS/venv/bin/python" -c 'import platform; print(platform.python_version())')"
    "$uv" pip install --quiet --python "$TCE_DADOS/venv/bin/python" -U "yt-dlp[default]" numpy \
      || morrer "falha ao instalar o yt-dlp"
    if [ ! -x "$TCE_DADOS/bin/deno" ]; then
      local arq_deno=x86_64; mac_arm && arq_deno=aarch64
      local tmp; tmp="$(mktemp -d)"
      curl -fsSL -o "$tmp/deno.zip" \
        "https://github.com/denoland/deno/releases/latest/download/deno-$arq_deno-apple-darwin.zip" \
        && unzip -o -q "$tmp/deno.zip" -d "$TCE_DADOS/bin" && chmod +x "$TCE_DADOS/bin/deno" \
        || aviso "não consegui baixar o deno (o YouTube pode recusar alguns downloads)"
      rm -rf "$tmp"
    fi
    export PATH="$TCE_DADOS/bin:$PATH"
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
  elif [ -d "$TCE_APP/.git" ] && tem_git; then
    git -C "$TCE_APP" pull --ff-only --quiet || aviso "não consegui atualizar (seguindo com a versão atual)"
  elif tem_git && ! eh_mac; then
    rm -rf "$TCE_APP"
    git clone --depth 1 --quiet "$REPO_URL" "$TCE_APP" || morrer "não consegui baixar os scripts de $REPO_URL"
  else
    local tmp; tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/app.zip" "$REPO_URL/archive/HEAD.zip" || morrer "não consegui baixar os scripts de $REPO_URL"
    unzip -q "$tmp/app.zip" -d "$tmp/x"
    rm -rf "$TCE_APP"
    mv "$tmp"/x/* "$TCE_APP"
    rm -rf "$tmp"
  fi
  chmod +x "$TCE_APP/tce/tce" "$TCE_APP/tce/motor.py" 2>/dev/null || true
  ok "$TCE_APP"
}

tem_git() {  # no Mac sem as ferramentas do Xcode o "git" só abre um aviso pedindo instalação
  command -v git >/dev/null 2>&1 || return 1
  ! eh_mac || xcode-select -p >/dev/null 2>&1
}

baixar_binarios_mac() {
  local arq=x86_64; mac_arm && arq=arm64
  passo "Programas prontos para Mac ($arq): whisper.cpp e ffmpeg"
  local nome marca="$TCE_DADOS/bin/.versao_binarios_mac" novo=""
  if [ -n "${TCE_SO_WHISPER:-}" ] || [ "$(cat "$marca" 2>/dev/null)" != "$VERSAO_BIN_MAC" ]; then novo=1; fi
  for nome in whisper-cli ffmpeg ffprobe; do
    if [ -n "$novo" ] || [ ! -x "$TCE_DADOS/bin/$nome" ]; then
      curl -fL --retry 5 --progress-bar -o "$TCE_DADOS/bin/$nome.part" "$URL_BIN_MAC/$nome-macos-$arq" \
        || morrer "não consegui baixar $nome para Mac ($URL_BIN_MAC)"
      mv -f "$TCE_DADOS/bin/$nome.part" "$TCE_DADOS/bin/$nome"
      chmod +x "$TCE_DADOS/bin/$nome"
      xattr -d com.apple.quarantine "$TCE_DADOS/bin/$nome" 2>/dev/null || true
    fi
  done
  echo "$VERSAO_BIN_MAC" > "$marca"
  mkdir -p "$TCE_DADOS/whisper.cpp/samples"
  [ -s "$TCE_DADOS/whisper.cpp/samples/jfk.wav" ] || curl -fsSL -o "$TCE_DADOS/whisper.cpp/samples/jfk.wav" \
    "https://raw.githubusercontent.com/ggml-org/whisper.cpp/$WHISPER_VERSAO/samples/jfk.wav"
  if [ "$arq" = arm64 ]; then ok "whisper-cli (usa a GPU do Mac), ffmpeg e ffprobe"
  else ok "whisper-cli, ffmpeg e ffprobe (Mac Intel)"; fi
}

compilar_whisper() {
  if eh_mac; then baixar_binarios_mac; return; fi
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
  eh_mac && bash_bin=/bin/bash          # o bash do próprio macOS (existe em todo Mac)
  cat > "$BIN_GLOBAL/tce" <<EOF
#!$bash_bin
exec "$bash_bin" "$TCE_APP/tce/tce" "\$@"
EOF
  chmod +x "$BIN_GLOBAL/tce"
  ok "comando 'tce'"

  if eh_mac; then
    # ~/.local/bin no PATH (zsh é o padrão do Mac; no bash, sem esconder um ~/.profile existente)
    local rcs=("$HOME/.zshrc") rc
    if [ -f "$HOME/.bash_profile" ]; then rcs+=("$HOME/.bash_profile")
    elif [ -f "$HOME/.profile" ]; then rcs+=("$HOME/.profile")
    elif [[ "${SHELL:-}" == */bash ]]; then rcs+=("$HOME/.bash_profile"); fi
    for rc in "${rcs[@]}"; do
      grep -qs 'TCE Transcritor' "$rc" \
        || printf '\n# TCE Transcritor\nexport PATH="$HOME/.local/bin:$PATH"\n' 2>/dev/null >> "$rc" \
        || aviso "não consegui pôr o tce no PATH em $rc"
    done
    local atalho="$HOME/Desktop/TCE Transcritor.command"
    if [ -d "$HOME/Desktop" ] && printf '#!%s\nexec "%s" "%s" menu\n' "$bash_bin" "$bash_bin" \
         "$TCE_APP/tce/tce" > "$atalho" 2>/dev/null && chmod +x "$atalho"; then
      ok "atalho 'TCE Transcritor' na Mesa (dois cliques abre o menu)"
    else
      aviso "não consegui criar o atalho na Mesa (use o comando tce no Terminal)"
    fi
  fi

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
PARTES_NUVEM=16

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
    if [ -z "${TCE_COMPATIVEL:-}" ] && ! eh_mac; then
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
  res="$(TCE_MOSTRAR_RESULTADO=1 "$BIN_GLOBAL/tce" velocidade 2>&1 || true)"
  echo "$res" | grep -E "ao vivo .*fiel" || true
  fator="$(echo "$res" | awk '$1 == "RESULTADO" && $2 == "small" {print $3; exit}')"
  if [ -n "$fator" ] && awk "BEGIN {exit !($fator < 1.6)}"; then
    aviso "o modelo small é lento demais para ao vivo neste aparelho; usando o base"
    "$BIN_GLOBAL/tce" modelos baixar base || true
    sed -i.bak 's/^MODELO_AOVIVO=.*/MODELO_AOVIVO="base"/' "$CONFIG" && rm -f "$CONFIG.bak"
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
if [ -z "${TCE_TESTE:-}" ]; then
  "$BIN_GLOBAL/tce" diagnostico || aviso "o YouTube não respondeu agora; rode 'tce diagnostico' mais tarde"
fi

echo
cor "1;32" "Tudo pronto!"
if eh_mac; then
  echo
  echo "Como usar no Mac:"
  echo "  * Abra uma janela NOVA do Terminal (ou dê dois cliques em 'TCE Transcritor' na Mesa) e digite:  tce"
  echo "  * Ou direto:  tce aovivo LINK   |   tce gravada LINK   |   tce arquivo   |   tce ajuda"
  echo "  * Os TXT ficam em Downloads/TCE-Transcricoes."
  exit 0
fi
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
