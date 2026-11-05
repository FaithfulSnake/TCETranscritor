#!/usr/bin/env bash
# Roda dentro do container termux/termux-docker (CI): instala e transcreve de verdade.
# Uso: bash testes/termux-ci.sh URL_DA_AMOSTRA
set -euo pipefail
AMOSTRA_URL="$1"
cd "$HOME"
echo "== instalando (modo teste)"
TCE_TESTE=1 TCE_FONTE="$HOME/src" TCE_MODELOS="small" bash "$HOME/src/instalar.sh"
echo "== versões"
yt-dlp --version
(deno --version || node --version) | head -1
ffmpeg -version | head -1
ls -la "$HOME/.local/share/tce/bin" "$HOME/.local/share/tce/modelos"
echo "== transcrição de 3 min de fala em português, pelo comando tce"
curl -fsSL --retry 5 -o "$HOME/completo.mp3" "$AMOSTRA_URL"
ffmpeg -nostdin -loglevel error -y -i "$HOME/completo.mp3" -t 180 -c copy "$HOME/sessao-teste.mp3"
sed -i 's/^MODELO_FIEL=.*/MODELO_FIEL="small"/' "$HOME/.config/tce/config"
tce arquivo "$HOME/sessao-teste.mp3"
cp "$HOME"/TCE-Transcricoes/*.txt /saida/ 2>/dev/null || true
grep -qi catilina "$HOME"/TCE-Transcricoes/*.txt
echo "== velocidade"
tce velocidade || true
echo "== atalhos"
head -3 "$HOME/bin/termux-url-opener"
tce ajuda | head -4
echo "TERMUX OK"
