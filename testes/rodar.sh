#!/usr/bin/env bash
# Testes de ponta a ponta do motor, com o whisper.cpp de verdade.
# Variáveis: WHISPER, MODELO, VAD (caminhos), AMOSTRA (mp3 com fala em português,
# começando pela Primeira Catilinária), PY (python), SAIDA_TESTES (pasta).
set -uo pipefail
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
MOTOR="$RAIZ/tce/motor.py"
PY="${PY:-python3}"
T="${SAIDA_TESTES:-$(mktemp -d)}"
mkdir -p "$T"
comum=(--whisper "$WHISPER" --modelo "$MODELO" --vad-modelo "$VAD"
       --dados "$T/dados" --pasta-saida "$T/saida" --threads "${THREADS:-4}")
falhas=0
checar() {  # checar "descrição" comando...
  local desc="$1"; shift
  if "$@"; then echo "  OK    $desc"; else echo "  FALHA $desc"; falhas=$((falhas + 1)); fi
}
linhas_texto() { grep -c '^\[[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\] [^[]' "$1" || true; }
sem_duplicadas() { [ -z "$(grep '^\[' "$1" | sort | uniq -d)" ]; }

echo "== 1. arquivo de 5 min, blocos de 2 min"
"$PY" "$MOTOR" arquivo "$AMOSTRA" --titulo "Teste arquivo" --bloco 120 "${comum[@]}"
f="$T/saida/Teste arquivo - TRANSCRIÇÃO.txt"
checar "TXT criado" test -s "$f"
checar "pelo menos 5 parágrafos com minutagem" test "$(linhas_texto "$f")" -ge 5
checar "reconheceu 'Catilina'" grep -qi "catilina" "$f"
checar "rodapé de fim" grep -q "^Fim\." "$f"
checar "sem linhas duplicadas" sem_duplicadas "$f"

echo "== 2. silêncio não vira texto inventado"
ffmpeg -nostdin -loglevel error -y -f lavfi -i anullsrc=r=16000:cl=mono -t 60 "$T/silencio.wav"
"$PY" "$MOTOR" arquivo "$T/silencio.wav" --titulo "Teste silencio" "${comum[@]}"
checar "nenhum parágrafo no silêncio" test "$(linhas_texto "$T/saida/Teste silencio - TRANSCRIÇÃO.txt")" -eq 0

echo "== 3. retomada depois de Ctrl+C"
# outra pasta de dados (o mesmo arquivo já foi transcrito no teste 1); interrompe
# assim que o 1º bloco estiver salvo, como se o celular desligasse no meio
"$PY" "$MOTOR" arquivo "$AMOSTRA" --titulo "Teste retomada" --bloco 60 "${comum[@]}" --dados "$T/dados3" \
  > "$T/retomada1.log" 2>&1 &
pid=$!
for _ in $(seq 1 1200); do
  if grep -qs '"processado": [1-9]' "$T"/dados3/sessoes/*/estado.json; then kill -TERM "$pid"; break; fi
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.25
done
wait "$pid" || true
cat "$T/retomada1.log" | tail -3
"$PY" "$MOTOR" arquivo "$AMOSTRA" --titulo "Teste retomada" --bloco 60 "${comum[@]}" --dados "$T/dados3" | tee "$T/retomada.log"
g="$T/saida/Teste retomada - TRANSCRIÇÃO.txt"
checar "continuou de onde parou" grep -q "Continuando de onde parou" "$T/retomada.log"
checar "sem linhas duplicadas" sem_duplicadas "$g"
checar "sem aviso de interrupção no final" bash -c "! grep -q 'interrompida' \"$g\""
checar "rodapé de fim" grep -q "^Fim\." "$g"
checar "um arquivo só (não criou cópia)" test ! -e "$T/saida/Teste retomada - TRANSCRIÇÃO (2).txt"
"$PY" "$MOTOR" arquivo "$AMOSTRA" --titulo "Teste retomada" --bloco 60 "${comum[@]}" --dados "$T/dados3" | tee "$T/repetido.log"
checar "rodar de novo não refaz" grep -q "já foi transcrito" "$T/repetido.log"

echo "== 4. ao vivo simulado (a conexão cai aos 35 s e volta)"
FALSO_AUDIO="$AMOSTRA" FALSO_TRECHOS="35,35" FALSO_ESTADO="$T/falso.estado" \
  "$PY" "$MOTOR" aovivo "https://www.youtube.com/watch?v=falso123" \
  --ytdlp "$RAIZ/testes/yt-dlp-falso" "${comum[@]}"
h="$(ls "$T"/saida/*"AO VIVO.txt" | head -1)"
checar "TXT ao vivo criado" test -s "$h"
checar "texto ao vivo" test "$(linhas_texto "$h")" -ge 2
checar "marcou a queda de conexão" grep -q "conexão com a live caiu" "$h"
checar "registrou a última live" test -s "$T/dados/ultima_live"

echo "== 5. versão fiel da live gravada"
"$PY" "$MOTOR" fiel "${comum[@]}" --manter-audio
k="$(ls "$T"/saida/*" - FIEL.txt" | head -1)"
checar "TXT fiel criado (com o título da live)" test -s "$k"
checar "nome do TXT fiel vem do título" bash -c "ls \"$T\"/saida/'Sessão de teste ao vivo - 05-11-2026 - FIEL.txt' >/dev/null"
checar "texto fiel" test "$(linhas_texto "$k")" -ge 2
checar "áudio da live guardado (m4a)" bash -c "ls \"$T\"/saida/*AUDIO.m4a >/dev/null"

echo "== 6. nuvem: dividir + partes + juntar"
partes="$("$PY" "$MOTOR" dividir "$AMOSTRA" --partes 3 --min-parte 60)"
echo "$partes"
mkdir -p "$T/partes"
n="$("$PY" -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$partes")"
checar "dividiu em 3 partes" test "$n" -eq 3
for i in $(seq 0 $((n - 1))); do
  ini="$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])[int(sys.argv[2])]["inicio"])' "$partes" "$i")"
  fim="$("$PY" -c 'import json,sys; v=json.loads(sys.argv[1])[int(sys.argv[2])]["fim"]; print("" if v is None else v)' "$partes" "$i")"
  extra=(); [ -n "$fim" ] && extra=(--fim "$fim")
  [ "$i" -eq 1 ] && continue    # simula uma parte que falhou
  "$PY" "$MOTOR" arquivo "$AMOSTRA" --inicio "$ini" "${extra[@]}" --silencioso \
    --fragmento "$T/partes/$(printf 'parte-%03d.txt' "$i")" "${comum[@]}"
done
"$PY" "$MOTOR" juntar --pasta "$T/partes" --partes-json "$partes" --saida "$T/saida/nuvem.txt" \
  --titulo "Teste nuvem" --nome-modelo teste --duracao 300
checar "TXT da nuvem criado" test -s "$T/saida/nuvem.txt"
checar "marcou a parte que faltou" grep -q "não foi transcrito" "$T/saida/nuvem.txt"
checar "partes em ordem de tempo" bash -c "grep -o '^\[[0-9:]*\]' \"$T/saida/nuvem.txt\" | sort -c"

echo "== 7. legenda do YouTube (json3)"
"$PY" - "$MOTOR" <<'EOF'
import importlib.util, json, sys, tempfile, os
spec = importlib.util.spec_from_file_location("motor", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ev = {"events": [
  {"tStartMs": 0, "dDurationMs": 5000, "segs": [{"utf8": "até"}, {"utf8": " quando", "tOffsetMs": 300}]},
  {"tStartMs": 900, "segs": [{"utf8": "\n"}], "aAppend": 1},
  {"tStartMs": 1000, "segs": [{"utf8": "ó"}, {"utf8": " catilina", "tOffsetMs": 200}]},
  {"tStartMs": 9000, "segs": [{"utf8": "abusarás"}, {"utf8": " da nossa paciência", "tOffsetMs": 400}]}]}
p = os.path.join(tempfile.mkdtemp(), "legenda.pt.json3"); json.dump(ev, open(p, "w"))
pars = m.json3_paragrafos(p)
print(pars)
assert pars[0] == (0.0, "até quando ó catilina"), pars
assert pars[1][1] == "abusarás da nossa paciência", pars
print("  OK    legenda json3 vira parágrafos")
t = "MAS VIVES, E VIVES NÃO PARA CEDER. DESEJO SER CLEMENTE PARA CONVOSCO"
assert m.Limpeza().filtrar([(0, 1, t)])[0][2] == "Mas vives, e vives não para ceder. Desejo ser clemente para convosco"
assert m.Limpeza().filtrar([(0, 1, "Processo do TCE-MA, relator.")])[0][2] == "Processo do TCE-MA, relator."
print("  OK    trecho travado em MAIÚSCULAS volta ao normal")
EOF
[ $? -eq 0 ] || falhas=$((falhas + 1))

echo
echo "== Trechos dos resultados"
for x in "$f" "$h" "$k" "$T/saida/nuvem.txt"; do echo "--- $(basename "$x")"; sed -n '1,16p' "$x"; echo "..."; done
echo
if [ "$falhas" -gt 0 ]; then echo "FALHAS: $falhas"; exit 1; fi
echo "TODOS OS TESTES PASSARAM"
