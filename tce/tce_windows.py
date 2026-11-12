#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TCE Transcritor - comando `tce` para Windows (sem precisar de administrador).

Tudo fica em %LOCALAPPDATA%\\TCETranscritor: Python portátil, yt-dlp, deno, ffmpeg,
whisper.cpp e os modelos. Os TXT vão para Downloads\\TCE-Transcricoes.

    tce               menu
    tce ajuda         todos os comandos
"""
from __future__ import annotations

import getpass
import os
import signal
import subprocess
import sys
import time
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)        # o Python portátil não põe a pasta do script no caminho
import motor  # noqa: E402

BASE = os.environ.get("TCE_BASE") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "TCETranscritor")
BIN = os.path.join(BASE, "bin")
MODELOS = os.path.join(BASE, "modelos")
DADOS = os.path.join(BASE, "dados")
CONFIG = os.path.join(BASE, "config.ini")
TOKEN = os.path.join(BASE, "token.txt")
HF = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
HF_VAD = "https://huggingface.co/ggml-org/whisper-vad/resolve/main"
VAD_ARQ = "ggml-silero-v6.2.0.bin"
INSTALADOR = "https://raw.githubusercontent.com/FaithfulSnake/TCETranscritor/HEAD/instalar.ps1"

ARQ_MODELO = {
    "tiny": "ggml-tiny-q8_0.bin", "base": "ggml-base-q8_0.bin", "small": "ggml-small-q8_0.bin",
    "medium": "ggml-medium-q5_0.bin", "turbo": "ggml-large-v3-turbo-q5_0.bin",
    "turbo-q8": "ggml-large-v3-turbo-q8_0.bin", "large": "ggml-large-v3-q5_0.bin",
}

PADRAO = {
    "PASTA_SAIDA": os.path.join(os.path.expanduser("~"), "Downloads", "TCE-Transcricoes"),
    "MODELO_AOVIVO": "small",
    "MODELO_FIEL": "turbo",
    "THREADS": "",
    "FIEL_DEPOIS_DA_LIVE": "perguntar",
    "GUARDAR_AUDIO": "1",
    "PROMPT": "",
    "REPO": "FaithfulSnake/TCETranscritor",
    "MODELO_NUVEM": "turbo",
    "PARTES_NUVEM": "16",
}


def ler_config() -> dict:
    cfg = dict(PADRAO)
    try:
        with open(CONFIG, encoding="utf-8-sig") as f:   # o PowerShell 5.1 grava com BOM
            for linha in f:
                linha = linha.strip()
                if linha and not linha.startswith("#") and "=" in linha:
                    k, v = linha.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    if not cfg["THREADS"]:
        n = os.cpu_count() or 4
        cfg["THREADS"] = str(max(2, min(8, n // 2 if n >= 8 else n)))
    return cfg


CFG = ler_config()


def negrito(t: str) -> None:
    print(f"\033[1m{t}\033[0m", flush=True)


def titulo(t: str) -> None:
    print()
    negrito(f"== {t} ==")


def exe(nome: str) -> str:
    caminho = os.path.join(BIN, nome + ".exe")
    return caminho if os.path.exists(caminho) else nome


def preparar_ambiente() -> None:
    os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")
    os.environ.update({
        "TCE_DADOS": DADOS, "TCE_WHISPER": os.path.join(BASE, "whisper", "whisper-cli.exe"),
        "TCE_VAD": os.path.join(MODELOS, VAD_ARQ), "TCE_THREADS": CFG["THREADS"],
        "TCE_SAIDA": CFG["PASTA_SAIDA"], "TCE_GUARDAR_AUDIO": CFG["GUARDAR_AUDIO"],
        "TCE_YTDLP": exe("yt-dlp"), "TCE_FFMPEG": exe("ffmpeg"), "TCE_FFPROBE": exe("ffprobe"),
        "TCE_REPO": CFG["REPO"], "TCE_TOKEN": TOKEN, "TCE_MODELO_NUVEM": CFG["MODELO_NUVEM"],
        "TCE_PARTES_NUVEM": CFG["PARTES_NUVEM"],
    })
    if CFG["PROMPT"]:
        os.environ["TCE_PROMPT"] = CFG["PROMPT"]


def baixar(url: str, destino: str) -> None:
    """Baixa com retomada (se a internet cair, continua de onde parou)."""
    if os.path.exists(destino) and os.path.getsize(destino) > 0:
        return
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    parcial = destino + ".part"
    print(f"Baixando {os.path.basename(destino)}...")
    for tentativa in range(1, 6):
        try:
            feito = os.path.getsize(parcial) if os.path.exists(parcial) else 0
            pedido = urllib.request.Request(url, headers={"User-Agent": "TCE-Transcritor",
                                                          **({"Range": f"bytes={feito}-"} if feito else {})})
            with urllib.request.urlopen(pedido, timeout=60) as r:
                if feito and r.status != 206:
                    feito = 0
                total = feito + int(r.headers.get("Content-Length") or 0)
                with open(parcial, "ab" if feito else "wb") as f:
                    ultimo = 0.0
                    while True:
                        bloco = r.read(1 << 20)
                        if not bloco:
                            break
                        f.write(bloco)
                        feito += len(bloco)
                        if time.time() - ultimo > 1:
                            ultimo = time.time()
                            pct = f"{100 * feito / total:5.1f}%" if total else ""
                            print(f"\r   {feito / 1e6:7.1f} MB {pct}", end="", flush=True)
            print()
            os.replace(parcial, destino)
            return
        except OSError as e:
            print(f"\n   falhou ({e}); tentando de novo ({tentativa}/5)...")
            time.sleep(3 * tentativa)
    raise SystemExit(f"Não consegui baixar {url}")


def garantir_modelo(nome: str) -> str:
    arq = ARQ_MODELO.get(nome, nome)
    destino = os.path.join(MODELOS, arq)
    baixar(f"{HF}/{arq}", destino)
    try:
        baixar(f"{HF_VAD}/{VAD_ARQ}", os.path.join(MODELOS, VAD_ARQ))
    except SystemExit:
        print("(sem o detector de voz: segue assim mesmo)")
    return destino


def motor_rodar(args: list[str]) -> int:
    try:
        rc = motor.principal(args)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if not isinstance(e.code, int) and e.code:
            print(e.code)
    except KeyboardInterrupt:
        print("\n>> Interrompido.")
        rc = 130
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    return rc or 0


def ler_link() -> str:
    print("Copie o link do YouTube e cole aqui (botão direito do mouse ou Ctrl+V).")
    try:
        return input("Link: ").strip().strip('"')
    except EOFError:
        return ""


def comum(nome_modelo: str) -> list[str]:
    return ["--modelo", garantir_modelo(nome_modelo), "--nome-modelo", nome_modelo]


# ---------------------------------------------------------------------------

def cmd_aovivo(args):
    link = args[0] if args else ler_link()
    if not link:
        return 1
    titulo(f"AO VIVO - modelo {CFG['MODELO_AOVIVO']}")
    rc = motor_rodar(["aovivo", link] + comum(CFG["MODELO_AOVIVO"]) + args[1:])
    if rc == 3:
        if input("Fazer a transcrição integral (vídeo gravado) agora? [S/n] ").strip().lower() != "n":
            return cmd_gravada([link])
        return 0
    ultima = os.path.join(DADOS, "ultima_live")
    if rc != 0 or not os.path.exists(ultima):
        return rc
    with open(ultima, encoding="utf-8") as f:
        sessao = f.read().strip()
    escolha = CFG["FIEL_DEPOIS_DA_LIVE"]
    if escolha == "perguntar":
        print()
        negrito("A live acabou. O rascunho AO VIVO está salvo.")
        print("Fazer agora a versão FIEL (modelo grande, transcrição integral)?")
        print("  1) Neste computador")
        print("  2) Na nuvem do GitHub (precisa ter rodado 'tce nuvem-configurar')")
        print("  0) Agora não (depois: 'tce fiel')")
        try:
            escolha = input("Escolha [1]: ").strip() or "1"
        except EOFError:
            escolha = "0"
    if escolha in ("1", "celular", "pc"):
        return cmd_fiel([sessao])
    if escolha in ("2", "nuvem"):
        return cmd_nuvem([sessao])
    print("Ok. Quando quiser: tce fiel")
    return 0


def cmd_gravada(args):
    link = args[0] if args else ler_link()
    if not link:
        return 1
    titulo(f"VÍDEO GRAVADO - versão fiel (modelo {CFG['MODELO_FIEL']})")
    rc = motor_rodar(["gravada", link] + comum(CFG["MODELO_FIEL"]) + args[1:])
    if rc == 3 and input("Ela está ao vivo. Transcrever AO VIVO agora? [S/n] ").strip().lower() != "n":
        return cmd_aovivo([link])
    return rc


def cmd_rapida(args):
    link = args[0] if args else ler_link()
    titulo(f"VÍDEO GRAVADO - versão rápida (modelo {CFG['MODELO_AOVIVO']})")
    return motor_rodar(["gravada", link] + comum(CFG["MODELO_AOVIVO"]) +
                       ["--sufixo", "RÁPIDA", "--tipo", "VÍDEO GRAVADO (versão rápida)"] + args[1:])


MIDIA = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".3gp", ".m4a", ".mp3", ".opus",
         ".ogg", ".wav", ".aac", ".amr", ".flac", ".wma")


def escolher_arquivo() -> str:
    casa = os.path.expanduser("~")
    pastas = [os.path.join(casa, p) for p in ("Downloads", "Videos", "Music", "Desktop", "Documents")]
    achados = []
    for pasta in pastas:
        base_prof = pasta.rstrip(os.sep).count(os.sep)
        for raiz, dirs, arqs in os.walk(pasta):
            if raiz.count(os.sep) - base_prof >= 3:
                dirs[:] = []
            for a in arqs:
                if a.lower().endswith(MIDIA):
                    c = os.path.join(raiz, a)
                    try:
                        st = os.stat(c)
                    except OSError:
                        continue
                    if st.st_size > 100_000:
                        achados.append((st.st_mtime, st.st_size, c))
    achados.sort(reverse=True)
    achados = achados[:15]
    if achados:
        print("Vídeos e áudios mais recentes deste computador:")
        for i, (_, tam, c) in enumerate(achados, 1):
            print(f"  {i:2d}) {os.path.basename(c)}  ({tam / 1e6:.0f} MB)")
        op = input("Número do arquivo (Enter para colar o caminho): ").strip()
        if op.isdigit() and 1 <= int(op) <= len(achados):
            return achados[int(op) - 1][2]
    print("Dica: no Explorer, segure Shift, clique com o botão direito no arquivo > 'Copiar como caminho'.")
    return input("Caminho do arquivo: ").strip().strip('"')


def cmd_arquivo(args):
    arq = args[0] if args else escolher_arquivo()
    arq = os.path.expanduser(arq.strip('"'))
    if not os.path.isfile(arq):
        print(f"Arquivo não encontrado: {arq}")
        return 1
    titulo(f"ARQUIVO - modelo {CFG['MODELO_FIEL']}")
    return motor_rodar(["arquivo", arq] + comum(CFG["MODELO_FIEL"]) + args[1:])


def cmd_fiel(args):
    titulo(f"VERSÃO FIEL da live gravada (modelo {CFG['MODELO_FIEL']})")
    return motor_rodar(["fiel"] + args[:1] + comum(CFG["MODELO_FIEL"]) + args[1:])


def cmd_legenda(args):
    link = args[0] if args else ler_link()
    titulo("LEGENDA DO YOUTUBE")
    return motor_rodar(["legenda", link] + args[1:])


def cmd_nuvem(args):
    if not os.path.exists(TOKEN):
        print("Primeiro configure o acesso ao GitHub (uma vez só): tce nuvem-configurar")
        return 1
    alvo = args[0] if args else ""
    if alvo in ("fiel", "live"):
        ultima = os.path.join(DADOS, "ultima_live")
        alvo = open(ultima, encoding="utf-8").read().strip() if os.path.exists(ultima) else ""
    if not alvo:
        alvo = ler_link()
    titulo(f"NUVEM - transcrição integral no GitHub Actions (modelo {CFG['MODELO_NUVEM']})")
    return motor_rodar(["nuvem", alvo, "--token-arquivo", TOKEN] + args[1:])


def cmd_nuvem_configurar(args):
    titulo("Configurar a transcrição na nuvem (GitHub Actions, grátis)")
    print(f"""Crie uma chave de acesso (token) no navegador:
 1. Abra: https://github.com/settings/personal-access-tokens/new
 2. Token name: TCE Transcritor     Expiration: 1 year (ou o máximo)
 3. Repository access: "Only select repositories" > {CFG['REPO']}
 4. Permissions > Repository permissions:  Actions: Read and write   Contents: Read and write
 5. Generate token > copie o código (começa com github_pat_)
""")
    token = getpass.getpass("Cole o token aqui (não aparece na tela) e tecle Enter: ").strip()
    if not token:
        print("Nenhum token colado.")
        return 1
    os.makedirs(BASE, exist_ok=True)
    with open(TOKEN, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    print("Testando...")
    rc = motor_rodar(["nuvem-testar", "--token-arquivo", TOKEN])
    if rc == 0:
        negrito("Pronto! Agora use: tce nuvem LINK")
    return rc


def cmd_velocidade(args):
    amostra = os.path.join(BASE, "amostras", "jfk.wav")
    modelos = [f"{n}={os.path.join(MODELOS, a)}" for n, a in ARQ_MODELO.items()
               if os.path.exists(os.path.join(MODELOS, a))]
    if not modelos or not os.path.exists(amostra):
        print("Falta a amostra ou algum modelo. Rode: tce atualizar")
        return 1
    titulo("Velocidade neste computador")
    rc = motor_rodar(["velocidade", "--amostra", amostra] + modelos)
    print(f"\nAo vivo usa: {CFG['MODELO_AOVIVO']}    Versão fiel usa: {CFG['MODELO_FIEL']}")
    print(f"Para trocar, edite {CONFIG}")
    return rc


def cmd_modelos(args):
    if len(args) >= 2 and args[0] == "baixar":
        garantir_modelo(args[1])
        negrito(f"Modelo {args[1]} pronto.")
        return 0
    titulo("Modelos")
    for nome, arq in ARQ_MODELO.items():
        c = os.path.join(MODELOS, arq)
        print(f"  [{'baixado' if os.path.exists(c) else '  --   '}] {nome}")
    print("\n  small: AO VIVO · turbo: versão FIEL (recomendado) · large: o mais preciso, lento")
    print("Baixar: tce modelos baixar NOME")
    return 0


def cmd_diagnostico(args):
    titulo("Diagnóstico")
    for nome in ("yt-dlp", "deno", "ffmpeg"):
        try:
            v = subprocess.run([exe(nome), "--version" if nome != "ffmpeg" else "-version"],
                               capture_output=True, text=True, timeout=60).stdout.splitlines()[0]
        except Exception:
            v = "NÃO ENCONTRADO (rode: tce atualizar)"
        print(f"{nome:8s} {v}")
    w = os.environ["TCE_WHISPER"]
    print(f"whisper  {w if os.path.exists(w) else 'NÃO ENCONTRADO (rode: tce atualizar)'}")
    print(f"modelos  {', '.join(sorted(os.listdir(MODELOS))) if os.path.isdir(MODELOS) else '-'}")
    print(f"TXT em   {CFG['PASTA_SAIDA']}")
    print("\nTestando o acesso ao YouTube...")
    r = subprocess.run([exe("yt-dlp"), "--no-warnings", "--simulate", "-f", "ba/b", "--print",
                        "%(title)s [%(format_id)s]", "https://www.youtube.com/watch?v=jNQXAC9IVRw"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        negrito(f"YouTube OK: {r.stdout.strip()}")
        return 0
    print(motor.explicar_erro_ytdlp(r.stderr))
    return 1


def cmd_atualizar(args):
    titulo("Atualizando")
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                            f"[Net.ServicePointManager]::SecurityProtocol=3072; irm {INSTALADOR} | iex"])


def cmd_pasta(args):
    os.makedirs(CFG["PASTA_SAIDA"], exist_ok=True)
    print(CFG["PASTA_SAIDA"])
    try:
        os.startfile(CFG["PASTA_SAIDA"])  # abre no Explorer
    except Exception:
        pass
    return 0


def cmd_ajuda(args):
    print(f"""TCE Transcritor - transcrição de lives e vídeos (grátis, roda no seu computador)

  tce                     menu
  tce aovivo LINK         live em tempo real (texto aparece na hora)
  tce gravada LINK        vídeo gravado: transcrição integral fiel (modelo {CFG['MODELO_FIEL']})
  tce rapida LINK         vídeo gravado com o modelo rápido ({CFG['MODELO_AOVIVO']})
  tce legenda LINK        legenda do próprio YouTube em TXT (segundos)
  tce arquivo [CAMINHO]   vídeo/áudio que já está no computador (sem caminho: mostra a lista)
  tce fiel                refaz a última live gravada com o modelo grande
  tce nuvem LINK          transcrição integral no GitHub Actions
  tce nuvem-configurar    liga a nuvem (uma vez só)
  tce velocidade          mede a velocidade dos modelos neste computador
  tce modelos [baixar N]  lista/baixa modelos (base, small, turbo, large)
  tce diagnostico         confere yt-dlp, YouTube, whisper e modelos
  tce atualizar           atualiza tudo
  tce pasta               abre a pasta dos TXT

Os TXT ficam em: {CFG['PASTA_SAIDA']}
Configuração:    {CONFIG}
Ctrl+C para parar (dá para continuar depois rodando o mesmo comando).""")
    return 0


def cmd_menu(args):
    opcoes = [("AO VIVO   - transcrever uma live agora", cmd_aovivo),
              ("GRAVADA   - transcrição integral de um vídeo (fiel)", cmd_gravada),
              ("RÁPIDA    - transcrição integral com o modelo rápido", cmd_rapida),
              ("ARQUIVO   - vídeo/áudio que está no computador", cmd_arquivo),
              ("LEGENDA   - legenda do YouTube em TXT (segundos)", cmd_legenda),
              ("NUVEM     - transcrição integral no GitHub", cmd_nuvem),
              ("FIEL      - refazer a última live gravada", cmd_fiel),
              ("Abrir a pasta dos TXT", cmd_pasta),
              ("Testar a velocidade deste computador", cmd_velocidade),
              ("Atualizar", cmd_atualizar),
              ("Ajuda", cmd_ajuda)]
    while True:
        print()
        negrito("==============================================")
        negrito("  TCE Transcritor")
        negrito("==============================================")
        for i, (texto, _) in enumerate(opcoes, 1):
            print(f"  {i:2d}) {texto}")
        print("   0) Sair")
        try:
            op = input("Escolha: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if op in ("0", "q", "sair"):
            return 0
        if op.isdigit() and 1 <= int(op) <= len(opcoes):
            try:
                opcoes[int(op) - 1][1]([])
            except KeyboardInterrupt:
                print("\n>> Interrompido.")
        else:
            print("Opção inválida.")


COMANDOS = {
    "menu": cmd_menu, "aovivo": cmd_aovivo, "ao-vivo": cmd_aovivo, "live": cmd_aovivo,
    "gravada": cmd_gravada, "video": cmd_gravada, "rapida": cmd_rapida, "rápida": cmd_rapida,
    "legenda": cmd_legenda, "arquivo": cmd_arquivo, "fiel": cmd_fiel, "nuvem": cmd_nuvem,
    "nuvem-configurar": cmd_nuvem_configurar, "velocidade": cmd_velocidade,
    "teste": cmd_velocidade, "modelos": cmd_modelos, "diagnostico": cmd_diagnostico,
    "diagnóstico": cmd_diagnostico, "atualizar": cmd_atualizar, "pasta": cmd_pasta,
    "ajuda": cmd_ajuda, "help": cmd_ajuda, "-h": cmd_ajuda, "--help": cmd_ajuda,
}


def main(argv: list[str]) -> int:
    if os.name == "nt":
        os.system("")   # liga as cores no cmd
    preparar_ambiente()
    if not argv:
        return cmd_menu([])
    cmd, resto = argv[0].lower(), argv[1:]
    if cmd in COMANDOS:
        return COMANDOS[cmd](resto) or 0
    if cmd.startswith(("http://", "https://", "www.", "youtu")):
        return cmd_gravada(argv)
    if os.path.isfile(argv[0]):
        return cmd_arquivo(argv)
    print(f"Comando desconhecido: {argv[0]}")
    return cmd_ajuda([]) or 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
