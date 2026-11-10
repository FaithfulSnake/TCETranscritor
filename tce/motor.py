#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TCE Transcritor - motor de transcrição.

Transforma em TXT o áudio de uma live (ao vivo), de um vídeo gravado do
YouTube ou de um arquivo do celular, usando o whisper.cpp (whisper-cli).

Como funciona:
  * o áudio vem direto do YouTube (yt-dlp) ou do arquivo (ffmpeg), já em
    16 kHz mono, sem precisar gravar o som do celular;
  * o áudio é cortado em blocos nos pontos de silêncio, para não partir
    palavras ao meio;
  * cada bloco vai para o whisper.cpp e o texto é gravado no TXT na hora,
    com a minutagem [hh:mm:ss];
  * se o celular desligar ou o processo for interrompido, o que já saiu
    está salvo, e rodar o mesmo comando de novo continua de onde parou.

Normalmente é chamado pelo comando `tce`; rode `motor.py -h` para ver tudo.
Usa só a biblioteca padrão do Python (numpy é opcional e só acelera).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave
from array import array

try:  # opcional: só deixa a procura de silêncio mais rápida
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

VERSAO = "1.0.0"
TAXA = 16000           # amostras por segundo
BPS = TAXA * 2         # bytes por segundo (PCM 16 bits mono)
LINHA = "=" * 72

PROMPT_PADRAO = (
    "Sessão do Tribunal de Contas do Estado do Maranhão, TCE-MA. "
    "Presidente, conselheiros, conselheiro substituto, Ministério Público de Contas, "
    "relator, voto, processo, prestação de contas, tomada de contas especial, "
    "parecer prévio, acórdão, denúncia, representação, sustentação oral."
)


# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------

def hms(segundos: float) -> str:
    s = max(0, int(segundos))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def duracao_humana(segundos: float) -> str:
    s = max(0, int(segundos))
    h, m = s // 3600, s % 3600 // 60
    if h:
        return f"{h}h{m:02d}min"
    if m:
        return f"{m} min"
    return f"{s} s"


def avisar(msg: str = "") -> None:
    print(msg, flush=True)


def erro(msg: str) -> None:
    print(f"\n[ERRO] {msg}", file=sys.stderr, flush=True)


def nome_arquivo(texto: str, limite: int = 100) -> str:
    """Nome de arquivo seguro para o armazenamento do Android."""
    texto = unicodedata.normalize("NFC", texto or "")
    texto = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", texto)
    texto = re.sub(r"\s+", " ", texto).strip(" .-")
    return texto[:limite].rstrip(" .-") or "transcricao"


def data_br(aaaammdd: str | None) -> str:
    if aaaammdd and re.fullmatch(r"\d{8}", aaaammdd):
        return f"{aaaammdd[6:8]}/{aaaammdd[4:6]}/{aaaammdd[0:4]}"
    return ""


def salvar_json(caminho: str, dados) -> None:
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, caminho)


def ler_json(caminho: str, padrao=None):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return padrao


def gravar_wav(caminho: str, pcm: bytes) -> None:
    with wave.open(caminho, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA)
        w.writeframes(pcm)


def caminho_livre(caminho: str) -> str:
    """Se o arquivo já existe, acrescenta (2), (3)..."""
    if not os.path.exists(caminho):
        return caminho
    base, ext = os.path.splitext(caminho)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def duracao_midia(ffprobe: str, caminho: str) -> float | None:
    if caminho.endswith(".pcm"):
        return os.path.getsize(caminho) / BPS
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", caminho],
            capture_output=True, text=True, timeout=120)
        return float(r.stdout.strip().splitlines()[0])
    except Exception:
        return None


def notificar(ativo: bool, titulo: str, texto: str, fixa: bool = True) -> None:
    """Notificação do Android (precisa do app Termux:API). Nunca trava o motor."""
    if not ativo or not shutil.which("termux-notification"):
        return
    cmd = ["termux-notification", "--id", "tce", "--title", titulo,
           "--content", texto, "--alert-once", "--priority", "low"]
    if fixa:
        cmd.append("--ongoing")
    try:
        subprocess.run(cmd, timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def avisar_galeria(caminho: str) -> None:
    """Faz o arquivo aparecer logo nos apps de arquivos (Termux:API, opcional)."""
    if shutil.which("termux-media-scan"):
        try:
            subprocess.run(["termux-media-scan", caminho], timeout=8,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# procura do ponto de corte (silêncio)
# ----------------------------------------------------------------------------

def ponto_de_corte(pcm, ini_s: float, fim_s: float) -> int:
    """Índice (em amostras) do trecho de 0,3 s mais silencioso entre ini_s e fim_s."""
    total = len(pcm) // 2
    a = max(0, int(ini_s * TAXA))
    b = min(total, int(fim_s * TAXA))
    if b - a < TAXA:
        return b
    quadro = 320   # 20 ms
    janela = 15    # 15 quadros = 300 ms
    n = (b - a) // quadro
    if _np is not None:
        x = _np.frombuffer(pcm, dtype="<i2", count=n * quadro, offset=a * 2).astype(_np.float32)
        energia = (x.reshape(n, quadro) ** 2).mean(axis=1)
        if n > janela:
            soma = _np.convolve(energia, _np.ones(janela, dtype=_np.float32), mode="valid")
            i = int(_np.argmin(soma)) + janela // 2
        else:
            i = int(_np.argmin(energia))
    else:
        amostras = array("h")
        amostras.frombytes(bytes(pcm[a * 2:(a + n * quadro) * 2]))
        if sys.byteorder == "big":
            amostras.byteswap()
        energia = []
        for k in range(n):
            energia.append(sum(v * v for v in amostras[k * quadro:(k + 1) * quadro:4]))
        if n > janela:
            soma = sum(energia[:janela])
            melhor, im = soma, 0
            for k in range(janela, n):
                soma += energia[k] - energia[k - janela]
                if soma < melhor:
                    melhor, im = soma, k - janela + 1
            i = im + janela // 2
        else:
            i = min(range(n), key=energia.__getitem__)
    return a + i * quadro + quadro // 2


# ----------------------------------------------------------------------------
# whisper.cpp
# ----------------------------------------------------------------------------

class Whisper:
    """Chama o whisper-cli para um arquivo WAV e devolve [(início, fim, texto)]."""

    def __init__(self, exe: str, modelo: str, vad_modelo: str | None, threads: int,
                 idioma: str = "pt", extra: list[str] | None = None):
        if not os.path.isfile(modelo):
            raise SystemExit(f"Modelo não encontrado: {modelo}\nRode: tce modelos")
        self.exe = exe
        self.modelo = modelo
        self.vad_modelo = vad_modelo if vad_modelo and os.path.isfile(vad_modelo) else None
        self.threads = max(1, int(threads))
        self.idioma = idioma
        self.extra = extra or []

    def _comando(self, wav: str, base: str, prompt: str, vad: bool) -> list[str]:
        cmd = [self.exe, "-m", self.modelo, "-f", wav, "-l", self.idioma,
               "-t", str(self.threads), "-oj", "-of", base, "-np"]
        if vad:
            cmd += ["--vad", "-vm", self.vad_modelo]
        if prompt:
            cmd += ["--prompt", prompt]
        return cmd + self.extra

    def transcrever(self, wav: str, prompt: str = "", duracao: float = 30.0):
        base = wav[:-4]
        js = base + ".json"
        tentativas = [True, True, False] if self.vad_modelo else [False, False]
        ultimo = ""
        for vad in tentativas:
            try:
                os.remove(js)
            except FileNotFoundError:
                pass
            limite = 600 + duracao * 40
            try:
                # sessão própria: o Ctrl+C do terminal não derruba o whisper no meio do bloco
                r = subprocess.run(self._comando(wav, base, prompt, vad),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=limite, start_new_session=True)
            except subprocess.TimeoutExpired:
                ultimo = f"o whisper passou de {int(limite)} s sem responder"
                continue
            if r.returncode == 0:
                return self._ler_json(js)
            saida = (r.stderr or b"").decode("utf-8", "replace").strip()
            ultimo = saida[-600:] or f"código de saída {r.returncode}"
            if r.returncode in (-4, 132):
                raise RuntimeError(
                    "o whisper.cpp usou instruções que este processador não tem "
                    "(Illegal instruction). Rode: tce reparar")
            if r.returncode in (-9, 137):
                ultimo = ("o Android encerrou o whisper (provavelmente falta de memória). "
                          "Feche outros apps ou use um modelo menor.")
            time.sleep(2)
        raise RuntimeError(f"o whisper falhou: {ultimo}")

    @staticmethod
    def _ler_json(js: str):
        if not os.path.exists(js):
            return []
        with open(js, "rb") as f:
            bruto = f.read().decode("utf-8", "replace")
        try:
            dados = json.loads(bruto, strict=False)
        except ValueError:
            dados = json.loads(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", bruto), strict=False)
        segs = []
        for s in dados.get("transcription", []) or []:
            off = s.get("offsets") or {}
            tx = (s.get("text") or "").strip()
            if tx:
                segs.append((float(off.get("from", 0)) / 1000.0,
                             float(off.get("to", 0)) / 1000.0, tx))
        try:
            os.remove(js)
        except OSError:
            pass
        return segs


# ----------------------------------------------------------------------------
# limpeza do texto e montagem dos parágrafos
# ----------------------------------------------------------------------------

_ALUCINACOES = [re.compile(p, re.I) for p in (
    r"amara\.org",
    r"^\W*(legendas?|legendado|legendagem|tradução|transcrição|revisão)\b.{0,80}$",
    r"\b(inscreva-se|se inscreva|ative o sininho|deixe (seu|o) like|curta e compartilhe)\b",
    r"^\W*obrigad[oa]s? (a todos )?por (assistir|ver|acompanhar)\b.{0,40}$",
    r"^\W*(tchau[\s,.!]*)+$",
    r"^\W*[\[(]?\s*(música|music|musica)\s*[\])]?\W*$",
    r"^[\W_]*$",
)]


def eh_alucinacao(texto: str) -> bool:
    """Frases que o Whisper inventa em silêncio ou ruído (créditos de legenda etc.)."""
    t = texto.strip()
    if len(t) > 160:
        return False
    return any(p.search(t) for p in _ALUCINACOES)


def maiusculas_demais(texto: str) -> bool:
    letras = [c for c in texto if c.isalpha()]
    return len(letras) >= 25 and sum(c.isupper() for c in letras) / len(letras) > 0.8


def frase_normal(texto: str) -> str:
    """'MAS VIVES, E VIVES' -> 'Mas vives, e vives' (só muda maiúsculas/minúsculas)."""
    t = texto.lower()
    return re.sub(r"(^|[.!?…]\s+)(\w)", lambda m: m.group(1) + m.group(2).upper(), t)


def compactar_repeticoes(texto: str) -> str:
    """'não não não não não' -> 'não' (laço típico do modelo)."""
    return re.sub(r"((?:\S+\s+){1,8}?)\1{3,}", r"\1", texto + " ").strip()


class Limpeza:
    def __init__(self):
        self.ultimo = ""
        self.repeticoes = 0

    def filtrar(self, segs):
        saida = []
        for t0, t1, tx in segs:
            tx = compactar_repeticoes(re.sub(r"\s+", " ", tx).strip())
            if not tx or eh_alucinacao(tx):
                continue
            if maiusculas_demais(tx):
                tx = frase_normal(tx)
            chave = re.sub(r"\W+", "", tx.lower())
            if chave == self.ultimo:
                self.repeticoes += 1
                if self.repeticoes >= 2:      # 3ª repetição seguida: descarta
                    continue
            else:
                self.ultimo, self.repeticoes = chave, 0
            saida.append((t0, t1, tx))
        return saida


def agrupar_paragrafos(segs, pausa: float = 2.5, alvo: int = 350, maximo: int = 900):
    """Junta os segmentos em parágrafos: quebra em pausas longas ou textos longos."""
    pars = []
    atual = None   # [início, texto, fim]
    for t0, t1, tx in segs:
        if atual is None:
            atual = [t0, tx, t1]
            continue
        fim_frase = atual[1].rstrip().endswith((".", "?", "!", "…", ":"))
        if (t0 - atual[2] >= pausa or (len(atual[1]) >= alvo and fim_frase)
                or len(atual[1]) >= maximo):
            pars.append((atual[0], atual[1]))
            atual = [t0, tx, t1]
        else:
            atual[1] += " " + tx
            atual[2] = t1
    if atual:
        pars.append((atual[0], atual[1]))
    return pars


def montar_prompt(base: str, contexto: str, limite: int = 220) -> str:
    contexto = contexto.strip()
    if maiusculas_demais(contexto):      # não "contamina" o próximo bloco com caixa alta
        contexto = frase_normal(contexto)
    if len(contexto) > limite:
        corte = contexto.find(" ", len(contexto) - limite)
        contexto = contexto[corte + 1:] if corte >= 0 else contexto[-limite:]
    return (base + " " + contexto).strip()


# ----------------------------------------------------------------------------
# saída TXT e estado (para retomar)
# ----------------------------------------------------------------------------

class Escritor:
    """Grava o TXT aos poucos, garantindo que cada pedaço chegue ao disco."""

    def __init__(self, caminho: str, cabecalho: list[str] | None, bytes_validos: int | None):
        self.caminho = caminho
        os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
        if bytes_validos is not None and os.path.exists(caminho):
            with open(caminho, "r+b") as f:   # descarta o que veio depois do último ponto salvo
                f.truncate(bytes_validos)
            self.f = open(caminho, "a", encoding="utf-8", newline="\n")
        else:
            self.f = open(caminho, "w", encoding="utf-8", newline="\n")
            if cabecalho:
                self._gravar("\n".join(cabecalho) + "\n\n")

    def _gravar(self, texto: str) -> None:
        self.f.write(texto)
        self.f.flush()
        try:
            os.fsync(self.f.fileno())
        except OSError:
            pass

    def paragrafos(self, pars) -> None:
        if pars:
            self._gravar("".join(f"[{hms(t)}] {tx}\n" for t, tx in pars))

    def linhas(self, linhas: list[str]) -> None:
        self._gravar("\n".join(linhas) + "\n")

    def tamanho(self) -> int:
        self.f.flush()
        return os.fstat(self.f.fileno()).st_size

    def fechar(self) -> None:
        try:
            self.f.close()
        except Exception:
            pass


class Estado:
    def __init__(self, pasta: str | None):
        self.pasta = pasta
        self.caminho = os.path.join(pasta, "estado.json") if pasta else None
        self.dados = (ler_json(self.caminho, {}) if self.caminho else {}) or {}

    def get(self, chave, padrao=None):
        return self.dados.get(chave, padrao)

    def atualizar(self, **kw) -> None:
        self.dados.update(kw)
        if self.caminho:
            salvar_json(self.caminho, self.dados)


def pasta_sessao(dados: str, chave: str) -> str:
    p = os.path.join(dados, "sessoes", nome_arquivo(chave, 80).replace(" ", "_"))
    os.makedirs(p, exist_ok=True)
    return p


# ----------------------------------------------------------------------------
# fontes de áudio (todas entregam PCM 16 kHz mono)
# ----------------------------------------------------------------------------

class FonteFFmpeg:
    """Decodifica qualquer arquivo de áudio/vídeo a partir de um ponto."""

    def __init__(self, ffmpeg: str, entrada: str, inicio: float = 0.0, fim: float | None = None):
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if inicio > 0:
            cmd += ["-ss", f"{inicio:.3f}"]
        cmd += ["-i", entrada]
        if fim is not None:
            cmd += ["-t", f"{max(0.0, fim - inicio):.3f}"]
        cmd += ["-vn", "-ac", "1", "-ar", str(TAXA), "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, start_new_session=True)
        self.acabou = False

    def ler(self, n: int) -> bytes:
        if self.acabou or n <= 0:
            return b""
        d = self.p.stdout.read(n)
        if len(d) < n:
            self.acabou = True
        return d

    def fechar(self) -> str:
        try:
            self.p.stdout.close()
        except Exception:
            pass
        try:
            self.p.terminate()
            _, err = self.p.communicate(timeout=10)
            return (err or b"").decode("utf-8", "replace").strip()
        except Exception:
            return ""


class FontePCM:
    """Lê PCM bruto de um arquivo. Com `seguir`, espera o arquivo crescer (ao vivo)."""

    def __init__(self, caminho: str, inicio: float = 0.0, fim: float | None = None, seguir=None):
        while seguir and seguir() and not os.path.exists(caminho):
            time.sleep(0.5)
        self.f = open(caminho, "rb")
        self.f.seek(int(inicio * TAXA) * 2)
        self.restante = None if fim is None else max(0, int((fim - inicio) * TAXA) * 2)
        self.seguir = seguir

    def ler(self, n: int) -> bytes:
        if self.restante is not None:
            n = min(n, self.restante)
        partes, falta = [], n
        while falta > 0:
            d = self.f.read(falta)
            if d:
                partes.append(d)
                falta -= len(d)
                continue
            if self.seguir and self.seguir():
                time.sleep(0.5)
                continue
            d = self.f.read(falta)   # o que chegou entre a última leitura e o fim da captura
            if d:
                partes.append(d)
                falta -= len(d)
                continue
            break
        dados = b"".join(partes)
        if self.restante is not None:
            self.restante -= len(dados)
        return dados

    def fechar(self) -> str:
        self.f.close()
        return ""


# ----------------------------------------------------------------------------
# YouTube (yt-dlp)
# ----------------------------------------------------------------------------

def explicar_erro_ytdlp(texto: str) -> str:
    t = texto or ""
    if "confirm you" in t and "bot" in t:
        return ("O YouTube pediu a verificação de robô para esta conexão. Tente de novo em "
                "alguns minutos ou troque de rede (Wi-Fi <-> dados móveis). Atualize com: tce atualizar")
    if "Private video" in t or "privado" in t:
        return "O vídeo é privado."
    if "Video unavailable" in t or "unavailable" in t.lower():
        return "O vídeo não está disponível (removido, privado ou com restrição)."
    if "Unable to download webpage" in t or "Failed to resolve" in t or "Temporary failure" in t:
        return "Sem internet ou o YouTube não respondeu. Confira a conexão e tente de novo."
    if "Requested format is not available" in t:
        return "O YouTube não ofereceu o formato de áudio esperado. Rode: tce atualizar"
    if "n challenge" in t or "JavaScript" in t or "js-runtimes" in t:
        return "O yt-dlp não conseguiu resolver o desafio do YouTube. Rode: tce atualizar"
    linhas = [l for l in t.strip().splitlines() if l.strip()]
    return linhas[-1] if linhas else "erro desconhecido no yt-dlp"


def info_video(ytdlp: str, url: str) -> dict:
    cmd = [ytdlp, "--no-warnings", "--skip-download", "--no-playlist",
           "--ignore-no-formats-error", "-J", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise RuntimeError("o YouTube demorou demais para responder")
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(explicar_erro_ytdlp(r.stderr.decode("utf-8", "replace")))
    j = json.loads(r.stdout)
    if j.get("_type") == "playlist" and j.get("entries"):
        j = j["entries"][0]
    return {
        "id": j.get("id") or hashlib.sha1(url.encode()).hexdigest()[:11],
        "titulo": j.get("title") or j.get("fulltitle") or url,
        "canal": j.get("channel") or j.get("uploader") or "",
        "duracao": j.get("duration"),
        "status": j.get("live_status") or ("is_live" if j.get("is_live") else "not_live"),
        "data": j.get("upload_date") or j.get("release_date") or "",
        "inicio_live": j.get("release_timestamp") or j.get("timestamp"),
        "url": j.get("webpage_url") or url,
    }


def status_live(ytdlp: str, url: str) -> str | None:
    try:
        r = subprocess.run([ytdlp, "--no-warnings", "--skip-download", "--no-playlist",
                            "--ignore-no-formats-error", "--print", "live_status", url],
                           capture_output=True, text=True, timeout=120)
        s = r.stdout.strip().splitlines()
        return s[-1].strip() if r.returncode == 0 and s else None
    except Exception:
        return None


class Captura(threading.Thread):
    """Grava o áudio da live (yt-dlp -> ffmpeg -> PCM em disco), reconectando se cair."""

    def __init__(self, url: str, pcm: str, ytdlp: str, ffmpeg: str, formato: str,
                 log: str, eventos: str, espera_maxima: float = 900):
        super().__init__(daemon=True)
        self.url, self.pcm, self.ytdlp, self.ffmpeg = url, pcm, ytdlp, ffmpeg
        self.formato, self.log, self.eventos = formato, log, eventos
        self.espera_maxima = espera_maxima
        self.parar = threading.Event()
        self.ativa = True
        self.procs: list[subprocess.Popen] = []
        self.ultimo_dado = time.time()
        self.recebendo = False
        self.erro = None
        self.lock = threading.Lock()

    def segundos_gravados(self) -> float:
        try:
            return os.path.getsize(self.pcm) / BPS
        except OSError:
            return 0.0

    def _evento(self, msg: str) -> None:
        with open(self.eventos, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": self.segundos_gravados(), "msg": msg,
                                "hora": time.strftime("%H:%M:%S")}, ensure_ascii=False) + "\n")

    def matar(self) -> None:
        with self.lock:
            for p in self.procs:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except Exception:
                    try:
                        p.terminate()
                    except Exception:
                        pass

    def _iniciar(self, log):
        y = subprocess.Popen(
            [self.ytdlp, "--no-warnings", "--no-playlist", "--no-part", "--quiet",
             "--hls-use-mpegts", "--wait-for-video", "30", "--retries", "10",
             "--fragment-retries", "10", "-f", self.formato, "-o", "-", self.url],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
            start_new_session=True)
        f = subprocess.Popen(
            [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", "pipe:0", "-vn", "-ac", "1", "-ar", str(TAXA),
             "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"],
            stdin=y.stdout, stdout=subprocess.PIPE, stderr=log, start_new_session=True)
        y.stdout.close()
        with self.lock:
            self.procs = [y, f]
        return f

    def _mostrar_erro_log(self) -> None:
        """Mostra (no máximo 1 vez por minuto) o último erro do yt-dlp enquanto tenta conectar."""
        try:
            with open(self.log, "rb") as f:
                f.seek(max(0, os.path.getsize(self.log) - 4000))
                linhas = [l.strip() for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        except OSError:
            return
        if not linhas:
            return
        agora = time.time()
        if linhas[-1] != getattr(self, "_ultimo_erro", None) or agora - getattr(self, "_quando_erro", 0) > 60:
            self._ultimo_erro, self._quando_erro = linhas[-1], agora
            avisar(f"   (tentando conectar à live: {explicar_erro_ytdlp(linhas[-1])})")

    def _vigia(self) -> None:
        """Se a live travar (sem áudio por 2 min), derruba a conexão para reconectar."""
        while self.ativa and not self.parar.is_set():
            time.sleep(10)
            if self.recebendo and time.time() - self.ultimo_dado > 120:
                self.recebendo = False
                self.matar()

    def run(self) -> None:
        threading.Thread(target=self._vigia, daemon=True).start()
        sem_audio_desde = None
        try:
            tamanho = os.path.getsize(self.pcm) if os.path.exists(self.pcm) else 0
            if tamanho % 2:
                with open(self.pcm, "r+b") as fp:
                    fp.truncate(tamanho - 1)
            with open(self.pcm, "ab") as saida, open(self.log, "ab") as log:
                while not self.parar.is_set():
                    ff = self._iniciar(log)
                    recebeu = 0
                    while True:
                        d = ff.stdout.read(BPS)
                        if not d:
                            break
                        if len(d) % 2:
                            d = d[:-1]
                        saida.write(d)
                        saida.flush()
                        recebeu += len(d)
                        self.ultimo_dado = time.time()
                        self.recebendo = True
                    self.recebendo = False
                    self.matar()
                    for p in self.procs:
                        try:
                            p.wait(timeout=15)
                        except Exception:
                            pass
                    if self.parar.is_set():
                        break
                    if not recebeu:
                        self._mostrar_erro_log()
                    estado = status_live(self.ytdlp, self.url)
                    if estado in ("is_live", "is_upcoming", None):
                        if recebeu:
                            sem_audio_desde = None
                        elif sem_audio_desde is None:
                            sem_audio_desde = time.time()
                        if sem_audio_desde and time.time() - sem_audio_desde > self.espera_maxima:
                            self.erro = "não foi possível reconectar à live por 15 minutos"
                            break
                        if recebeu:
                            self._evento("a conexão com a live caiu e foi retomada; pode faltar um trecho aqui")
                        self.parar.wait(5)
                        continue
                    break   # was_live / post_live / not_live: a live terminou
        except Exception as e:  # pragma: no cover
            self.erro = str(e)
        finally:
            self.ativa = False


# ----------------------------------------------------------------------------
# laço principal de transcrição
# ----------------------------------------------------------------------------

class Parada:
    """Controla o Ctrl+C: na live, o 1º para a captura e termina o que já foi gravado."""

    def __init__(self, ao_parar_captura=None):
        self.vezes = 0
        self.ao_parar_captura = ao_parar_captura

    def instalar(self) -> None:
        if self.ao_parar_captura:
            signal.signal(signal.SIGINT, self._tratar)

    def _tratar(self, sig, frame):
        self.vezes += 1
        if self.vezes == 1 and self.ao_parar_captura:
            avisar("\n>> Parando a captura. Vou terminar de transcrever o que já foi gravado.")
            avisar(">> (Ctrl+C de novo para sair agora; dá para continuar depois.)")
            self.ao_parar_captura()
        else:
            raise KeyboardInterrupt


def transcrever(fonte, whisper: Whisper, escritor: Escritor, estado: Estado, *,
                sessao: str, inicio: float = 0.0, total: float | None = None,
                bloco: float = 300.0, prompt_base: str = "", ao_vivo: bool = False,
                captura: Captura | None = None, eventos: str | None = None,
                notif: bool = False, titulo_notif: str = "TCE Transcritor",
                mostrar_texto: bool = True) -> float:
    """Lê o áudio em blocos, corta no silêncio, transcreve e grava. Devolve a posição final."""
    if ao_vivo:
        bloco = min(bloco, 28.0)
        ini_corte = max(5.0, bloco - 8.0)
        fim_corte = min(29.5, bloco + 3.0)
    else:
        ini_corte = bloco - min(30.0, 0.3 * bloco)
        fim_corte = bloco + min(30.0, 0.15 * bloco)
    buf = bytearray()
    pos = inicio                    # tempo (s) do primeiro byte de `buf`
    limpeza = Limpeza()
    contexto = estado.get("contexto", "")
    eventos_feitos = int(estado.get("eventos_feitos", 0))
    t_inicio = time.time()
    processado_agora = 0.0
    wav = os.path.join(sessao, "bloco.wav")

    while True:
        falta = int(fim_corte * TAXA) * 2 - len(buf)
        acabou = False
        if falta > 0:
            d = fonte.ler(falta)
            buf += d
            acabou = len(d) < falta
        if len(buf) % 2:
            del buf[-1:]
        dur = len(buf) / BPS
        if dur < 0.3:
            if acabou:
                break
            continue
        corte = len(buf) // 2 if acabou else ponto_de_corte(buf, ini_corte, fim_corte)
        pcm = bytes(buf[:corte * 2])
        del buf[:corte * 2]
        dur_bloco = corte / TAXA
        fim_bloco = pos + dur_bloco

        segs = []
        falhou = None
        if dur_bloco >= 0.3:
            gravar_wav(wav, pcm)
            try:
                brutos = whisper.transcrever(wav, montar_prompt(prompt_base, contexto), dur_bloco)
            except RuntimeError as e:
                # ao vivo, um bloco com erro não pode parar a captura: marca e segue
                if not ao_vivo or "Illegal instruction" in str(e):
                    raise
                falhou = str(e)
                brutos = []
            segs = limpeza.filtrar([(pos + a, pos + b, tx) for a, b, tx in brutos])
        pars = agrupar_paragrafos(segs)
        if falhou:
            avisar(f"   (erro ao transcrever este trecho: {falhou[-200:]})")
            pars.append((pos, "[trecho não transcrito ao vivo por erro; o áudio foi gravado "
                              "e entra na versão fiel]"))

        # marcadores de queda de conexão (ao vivo)
        if eventos and os.path.exists(eventos):
            with open(eventos, encoding="utf-8") as f:
                todos = [json.loads(l) for l in f if l.strip()]
            for ev in todos[eventos_feitos:]:
                if ev["t"] <= fim_bloco:
                    pars.append((ev["t"], f"[{ev['msg']} ({ev['hora']})]"))
                    eventos_feitos += 1
            pars.sort(key=lambda p: p[0])

        escritor.paragrafos(pars)
        if segs:
            contexto = (contexto + " " + " ".join(tx for _, _, tx in segs))[-600:]
        processado_agora += dur_bloco
        pos = fim_bloco
        estado.atualizar(processado=pos, txt_bytes=escritor.tamanho(),
                         contexto=contexto[-300:], eventos_feitos=eventos_feitos)

        if mostrar_texto:
            for t, tx in pars:
                avisar(f"[{hms(t)}] {tx}")
        decorrido = max(0.001, time.time() - t_inicio)
        vel = processado_agora / decorrido
        if ao_vivo and captura is not None:
            atraso = max(0.0, captura.segundos_gravados() - pos)
            situacao = "gravando" if captura.ativa else "captura encerrada"
            linha = f"   -- ao vivo ({situacao}): transcrito até {hms(pos)}, atraso {hms(atraso)}"
            if atraso > 300 and captura.ativa:
                linha += "  (o celular não está dando conta: use um modelo menor, veja 'tce velocidade')"
            avisar(linha)
            notificar(notif, titulo_notif, f"Ao vivo: transcrito até {hms(pos)} (atraso {hms(atraso)})")
        elif total:
            pct = min(100.0, 100.0 * pos / total)
            resto = max(0.0, total - pos) / max(vel, 0.01)
            avisar(f"   -- {hms(pos)} de {hms(total)} ({pct:.0f}%) · velocidade {vel:.1f}x "
                   f"· faltam ~{duracao_humana(resto)}")
            notificar(notif, titulo_notif, f"{pct:.0f}% · faltam ~{duracao_humana(resto)}")
        else:
            avisar(f"   -- transcrito até {hms(pos)} · velocidade {vel:.1f}x")
        if acabou and not buf:
            break
    try:
        os.remove(wav)
    except OSError:
        pass
    return pos


def cabecalho(tipo: str, titulo: str, canal: str = "", link: str = "", data: str = "",
              modelo: str = "", extras: list[str] | None = None,
              nota_tempo: str = "Os tempos [hh:mm:ss] indicam a posição no vídeo/áudio.") -> list[str]:
    linhas = [f"TRANSCRIÇÃO AUTOMÁTICA - {tipo}", "", f"Título: {titulo}"]
    if canal:
        linhas.append(f"Canal:  {canal}")
    if link:
        linhas.append(f"Link:   {link}")
    if data:
        linhas.append(f"Data:   {data}")
    linhas += extras or []
    linhas += [
        f"Gerado em {dt.datetime.now():%d/%m/%Y %H:%M} pelo TCE Transcritor "
        f"(whisper.cpp, modelo {modelo}).",
        "Texto gerado por computador, sem revisão humana: confira no vídeo/áudio os trechos citados.",
    ]
    if nota_tempo:
        linhas.append(nota_tempo)
    linhas.append(LINHA)
    return linhas


def rodape(pos: float, segundos_gastos: float, completo: bool = True) -> list[str]:
    if completo:
        return ["", LINHA,
                f"Fim. {hms(pos)} de áudio transcritos em {duracao_humana(segundos_gastos)} "
                f"({dt.datetime.now():%d/%m/%Y %H:%M})."]
    return ["", f"[Transcrição interrompida em {hms(pos)}. Para continuar, rode o mesmo comando de novo.]"]


# ----------------------------------------------------------------------------
# execução de uma transcrição completa (com retomada)
# ----------------------------------------------------------------------------

def preparar_saida(a, estado: Estado, titulo: str, sufixo: str) -> tuple[str, int | None, float]:
    """Decide o TXT de saída e se é para retomar. Devolve (caminho, bytes_validos, inicio)."""
    txt = estado.get("txt")
    if (not getattr(a, "refazer", False) and txt and os.path.exists(txt)
            and estado.get("txt_bytes") is not None and not estado.get("concluido")):
        avisar(f">> Continuando de onde parou ({hms(estado.get('processado', 0))}).")
        return txt, int(estado.get("txt_bytes")), float(estado.get("processado", 0))
    pasta = os.path.expanduser(a.pasta_saida)
    os.makedirs(pasta, exist_ok=True)
    txt = caminho_livre(os.path.join(pasta, f"{nome_arquivo(titulo)} - {sufixo}.txt"))
    for chave in ("txt_bytes",):           # zera só o progresso; mantém título, link etc.
        estado.dados.pop(chave, None)
    estado.atualizar(txt=txt, processado=0.0, concluido=False, contexto="", eventos_feitos=0)
    return txt, None, 0.0


def executar(a, fonte_fabrica, *, estado: Estado, sessao: str, titulo: str, sufixo: str,
             cab: list[str], total: float | None, ao_vivo=False, captura=None,
             eventos=None) -> str:
    feito = estado.get("txt")
    if (estado.get("concluido") and not getattr(a, "refazer", False)
            and feito and os.path.exists(feito)):
        avisar(f">> Isto já foi transcrito antes:\n   {feito}\n   (para fazer de novo, acrescente --refazer)")
        return feito
    txt, bytes_validos, inicio = preparar_saida(a, estado, titulo, sufixo)
    whisper = Whisper(a.whisper, a.modelo, a.vad_modelo, a.threads, a.idioma,
                      shlex.split(a.whisper_extra or ""))
    escritor = Escritor(txt, cab, bytes_validos)
    if bytes_validos is None:
        estado.atualizar(txt_bytes=escritor.tamanho())
    avisar(f">> Salvando em: {txt}")
    fonte = fonte_fabrica(inicio)
    t0 = time.time()
    completo = False
    pos = inicio
    try:
        pos = transcrever(fonte, whisper, escritor, estado, sessao=sessao, inicio=inicio,
                          total=total, bloco=a.bloco, prompt_base=a.prompt, ao_vivo=ao_vivo,
                          captura=captura, eventos=eventos, notif=a.notificar,
                          titulo_notif=f"TCE: {titulo[:40]}")
        completo = True
    except KeyboardInterrupt:
        avisar("\n>> Interrompido. O que já foi transcrito está salvo.")
    except RuntimeError as e:
        erro(str(e))
    finally:
        msg = fonte.fechar()
        if msg and not completo and "Broken pipe" not in msg:
            avisar(f"(ffmpeg: {msg[-300:]})")
        pos = float(estado.get("processado", pos))
        if completo:
            escritor.linhas(rodape(pos, time.time() - t0, True))
            estado.atualizar(concluido=True, txt_bytes=escritor.tamanho())
        else:
            escritor.linhas(rodape(pos, 0, False))   # não conta em txt_bytes: some ao retomar
        escritor.fechar()
        avisar_galeria(txt)
    if not completo:
        notificar(a.notificar, "TCE Transcritor", f"Interrompido em {hms(pos)}", fixa=False)
        raise SystemExit(2)
    notificar(a.notificar, "TCE Transcritor: pronto", os.path.basename(txt), fixa=False)
    avisar(f"\n>> Pronto! TXT salvo em:\n   {txt}")
    return txt


# ----------------------------------------------------------------------------
# comandos
# ----------------------------------------------------------------------------

def cmd_info(a) -> int:
    i = info_video(a.ytdlp, a.url)
    for k, v in i.items():
        print(f"{k.upper()}={shlex.quote(str(v if v is not None else ''))}")
    return 0


def cmd_arquivo(a) -> int:
    entrada = os.path.abspath(os.path.expanduser(a.entrada))
    if not os.path.isfile(entrada):
        raise SystemExit(f"Arquivo não encontrado: {entrada}")
    titulo = a.titulo or os.path.splitext(os.path.basename(entrada))[0]
    total_arquivo = duracao_midia(a.ffprobe, entrada)
    ini_corte = a.inicio or 0.0
    fim_corte = a.fim
    if a.fragmento:   # modo nuvem: só os parágrafos de um trecho, sem cabeçalho
        sessao = a.sessao or pasta_sessao(a.dados, f"frag-{os.getpid()}")
        os.makedirs(sessao, exist_ok=True)
        estado = Estado(None)
        whisper = Whisper(a.whisper, a.modelo, a.vad_modelo, a.threads, a.idioma,
                          shlex.split(a.whisper_extra or ""))
        escritor = Escritor(a.fragmento, None, None)
        fonte = (FontePCM(entrada, ini_corte, fim_corte) if entrada.endswith(".pcm")
                 else FonteFFmpeg(a.ffmpeg, entrada, ini_corte, fim_corte))
        fim_total = fim_corte if fim_corte is not None else total_arquivo
        transcrever(fonte, whisper, escritor, estado, sessao=sessao, inicio=ini_corte,
                    total=fim_total, bloco=a.bloco, prompt_base=a.prompt,
                    mostrar_texto=not a.silencioso)
        msg = fonte.fechar()
        escritor.fechar()
        if msg:
            avisar(f"(ffmpeg: {msg[-300:]})")
        return 0

    st = os.stat(entrada)
    chave = "arq-" + hashlib.sha1(f"{entrada}|{st.st_size}".encode()).hexdigest()[:10] + "-" + a.nome_modelo
    sessao = a.sessao or pasta_sessao(a.dados, chave)
    estado = Estado(sessao)
    cab = cabecalho(a.tipo or "ARQUIVO DE ÁUDIO", titulo, link=a.link or "",
                    data=a.data or "", modelo=a.nome_modelo,
                    extras=[f"Arquivo: {os.path.basename(entrada)}"] if not a.link else None)

    def fabrica(inicio):
        if entrada.endswith(".pcm"):
            return FontePCM(entrada, inicio, fim_corte)
        return FonteFFmpeg(a.ffmpeg, entrada, inicio, fim_corte)

    executar(a, fabrica, estado=estado, sessao=sessao, titulo=titulo,
             sufixo=a.sufixo or "TRANSCRIÇÃO", cab=cab, total=total_arquivo)
    return 0


def baixar_audio(a, url: str, pasta: str) -> str:
    prontos = [p for p in glob.glob(os.path.join(pasta, "audio.*"))
               if not p.endswith((".part", ".ytdl", ".json", ".tmp"))]
    if prontos:
        return prontos[0]
    avisar(">> Baixando o áudio do YouTube (só o áudio, para gastar pouca internet)...")
    cmd = [a.ytdlp, "--no-warnings", "--no-playlist", "--retries", "20",
           "--fragment-retries", "20", "--continue", "-f", a.formato_gravada,
           "-o", os.path.join(pasta, "audio.%(ext)s"), url]
    r = subprocess.run(cmd, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(explicar_erro_ytdlp(r.stderr.decode("utf-8", "replace")))
    prontos = [p for p in glob.glob(os.path.join(pasta, "audio.*"))
               if not p.endswith((".part", ".ytdl", ".json", ".tmp"))]
    if not prontos:
        raise RuntimeError("o download terminou mas o arquivo de áudio não apareceu")
    return prontos[0]


def cmd_gravada(a) -> int:
    try:
        info = info_video(a.ytdlp, a.url)
    except RuntimeError as e:
        erro(str(e))
        return 1
    avisar(f">> {info['titulo']}")
    if info["status"] in ("is_live", "is_upcoming"):
        avisar(">> Esta live ainda está no ar (ou vai começar). Use a opção AO VIVO: tce aovivo LINK")
        return 3
    if info["status"] == "post_live":
        avisar(">> ATENÇÃO: a live acabou há pouco e o YouTube ainda está processando o vídeo.\n"
               "   O download pode vir incompleto (às vezes só as últimas 2 horas).\n"
               "   Se você gravou a live com o TCE Transcritor, use: tce fiel\n"
               "   Ou tente de novo daqui a algumas horas.")
    sessao = a.sessao or pasta_sessao(a.dados, f"{info['id']}-{a.nome_modelo}")
    estado = Estado(sessao)
    if estado.get("concluido") and not a.refazer and estado.get("txt") and os.path.exists(estado.get("txt")):
        avisar(f">> Este vídeo já foi transcrito com este modelo:\n   {estado.get('txt')}\n"
               "   (para fazer de novo: tce gravada LINK --refazer)")
        return 0
    try:
        audio = baixar_audio(a, info["url"], sessao)
    except RuntimeError as e:
        erro(str(e))
        return 1
    total = duracao_midia(a.ffprobe, audio) or info.get("duracao")
    cab = cabecalho(a.tipo or "VÍDEO GRAVADO (versão fiel)", info["titulo"], info["canal"],
                    info["url"], data_br(info["data"]), a.nome_modelo,
                    extras=[f"Duração: {hms(total)}"] if total else None)
    executar(a, lambda ini: FonteFFmpeg(a.ffmpeg, audio, ini), estado=estado, sessao=sessao,
             titulo=info["titulo"], sufixo=a.sufixo or "FIEL", cab=cab, total=total)
    if not a.manter_audio:
        try:
            os.remove(audio)
        except OSError:
            pass
    return 0


def cmd_aovivo(a) -> int:
    try:
        info = info_video(a.ytdlp, a.url)
    except RuntimeError as e:
        erro(str(e))
        return 1
    avisar(f">> {info['titulo']}")
    if info["status"] in ("was_live", "not_live", "post_live") and not a.forcar:
        avisar(">> Este vídeo não está ao vivo agora. Para vídeos gravados use: tce gravada LINK")
        return 3
    if info["status"] == "is_upcoming":
        avisar(">> A live ainda não começou. Vou esperar e começar sozinho quando entrar no ar.")
    sessao = a.sessao or pasta_sessao(a.dados, f"{info['id']}-aovivo")
    estado = Estado(sessao)
    pcm = os.path.join(sessao, "audio16k.pcm")
    eventos = os.path.join(sessao, "eventos.jsonl")
    if estado.get("concluido") and not a.refazer:
        avisar(">> Esta live já foi transcrita ao vivo. Para a versão fiel: tce fiel")
        return 0
    if estado.get("processado") and os.path.exists(pcm):
        with open(eventos, "a", encoding="utf-8") as f:   # retomada depois de uma queda
            f.write(json.dumps({"t": os.path.getsize(pcm) / BPS,
                                "msg": "a gravação foi interrompida e retomada; pode faltar um trecho aqui",
                                "hora": time.strftime("%H:%M:%S")}, ensure_ascii=False) + "\n")
    estado.atualizar(tipo="aovivo", url=info["url"], titulo=info["titulo"], canal=info["canal"],
                     data=info["data"], pcm=pcm, id=info["id"])
    captura = Captura(info["url"], pcm, a.ytdlp, a.ffmpeg, a.formato_aovivo,
                      os.path.join(sessao, "captura.log"), eventos)
    inicio_cap = dt.datetime.now()
    extras = [f"Gravação pelo celular iniciada em {inicio_cap:%d/%m/%Y às %H:%M:%S}."]
    if info.get("inicio_live"):
        try:
            ini_live = dt.datetime.fromtimestamp(int(info["inicio_live"]))
            if ini_live <= inicio_cap:
                extras.append(f"A live começou às {ini_live:%H:%M:%S}.")
        except Exception:
            pass
    cab = cabecalho("AO VIVO (rascunho em tempo real)", info["titulo"], info["canal"],
                    info["url"], data_br(info["data"]) or f"{inicio_cap:%d/%m/%Y}",
                    a.nome_modelo, extras,
                    nota_tempo="Os tempos [hh:mm:ss] contam a partir do início da gravação pelo celular.")
    if "-bs" not in (a.whisper_extra or "") and "--beam-size" not in (a.whisper_extra or ""):
        a.whisper_extra = ("-bs 1 " + (a.whisper_extra or "")).strip()   # rascunho: decodificação rápida
    parada = Parada(lambda: (captura.parar.set(), captura.matar()))
    parada.instalar()
    captura.start()
    avisar(">> Captando o áudio da live. O texto aparece aqui a cada ~25 segundos.")
    avisar(">> Pode apagar a tela. Para parar: Ctrl+C (o Termux tem a tecla CTRL na barra de cima).")
    try:
        executar(a, lambda ini: FontePCM(pcm, ini, seguir=lambda: captura.ativa),
                 estado=estado, sessao=sessao, titulo=info["titulo"], sufixo="AO VIVO",
                 cab=cab, total=None, ao_vivo=True, captura=captura, eventos=eventos)
    finally:
        captura.parar.set()
        captura.matar()
    if captura.erro:
        avisar(f">> Aviso da captura: {captura.erro}")
    with open(os.path.join(a.dados, "ultima_live"), "w", encoding="utf-8") as f:
        f.write(sessao + "\n")
    return 0


def sessoes_aovivo(dados: str) -> list[str]:
    pastas = glob.glob(os.path.join(dados, "sessoes", "*-aovivo"))
    pastas = [p for p in pastas if os.path.exists(os.path.join(p, "audio16k.pcm"))]
    return sorted(pastas, key=lambda p: os.path.getmtime(os.path.join(p, "audio16k.pcm")), reverse=True)


def comprimir_audio(ffmpeg: str, pcm: str, destino: str) -> bool:
    r = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "s16le", "-ar", str(TAXA), "-ac", "1", "-i", pcm,
                        "-c:a", "aac", "-b:a", "48k", destino])
    return r.returncode == 0 and os.path.exists(destino) and os.path.getsize(destino) > 0


def cmd_fiel(a) -> int:
    if a.sessao_aovivo:
        origem = os.path.abspath(os.path.expanduser(a.sessao_aovivo))
    else:
        lista = sessoes_aovivo(a.dados)
        if not lista:
            avisar("Nenhuma live gravada encontrada. Para um vídeo do YouTube use: tce gravada LINK")
            return 1
        origem = lista[0]
    info = ler_json(os.path.join(origem, "estado.json"), {}) or {}
    pcm = os.path.join(origem, "audio16k.pcm")
    if not os.path.exists(pcm):
        avisar(f"Áudio da live não encontrado em {origem}")
        return 1
    titulo = info.get("titulo") or os.path.basename(origem)
    avisar(f">> Versão fiel de: {titulo}")
    sessao = pasta_sessao(a.dados, os.path.basename(origem).replace("-aovivo", "") + f"-fiel-{a.nome_modelo}")
    estado = Estado(sessao)
    total = os.path.getsize(pcm) / BPS
    cab = cabecalho("LIVE GRAVADA PELO CELULAR (versão fiel)", titulo, info.get("canal", ""),
                    info.get("url", ""), data_br(info.get("data")), a.nome_modelo,
                    extras=[f"Duração gravada: {hms(total)}."],
                    nota_tempo="Os tempos [hh:mm:ss] contam a partir do início da gravação pelo celular.")
    txt = executar(a, lambda ini: FontePCM(pcm, ini), estado=estado, sessao=sessao,
                   titulo=titulo, sufixo="FIEL", cab=cab, total=total)
    if a.guardar_audio:
        destino = caminho_livre(os.path.splitext(txt)[0].replace(" - FIEL", "") + " - AUDIO.m4a")
        avisar(">> Guardando o áudio da sessão (m4a) junto do TXT...")
        if comprimir_audio(a.ffmpeg, pcm, destino):
            avisar(f"   {destino}")
            avisar_galeria(destino)
            if not a.manter_audio:
                os.remove(pcm)
    return 0


# ---- legenda automática do YouTube -----------------------------------------

def paragrafos_legenda(palavras):
    """Agrupa pedaços de legenda [(início, texto, fim)] em parágrafos.

    A legenda automática do YouTube marca troca de quem fala com '>>':
    cada '>>' abre um parágrafo novo, iniciado por travessão. Fora isso,
    quebra em pausas de mais de 2,5 s ou em textos longos (no fim da frase).
    """
    pars, atual, t_atual, fim_ant = [], "", None, None

    def fechar():
        texto = re.sub(r"\s+", " ", atual).strip()
        if texto and texto != "—":
            pars.append((t_atual, texto))

    for t, u, fim in palavras:
        u = re.sub(r"\[[^\]]{1,40}\]", " ", u)    # marcas de som: [aplausos], [limpando a garganta]...
        for k, pedaco in enumerate(u.split(">>")):
            troca = k > 0
            if atual and (troca or t - fim_ant > 2.5 or len(atual) > 900
                          or (len(atual) > 450 and atual.rstrip().endswith((".", "?", "!")))):
                fechar()
                atual, t_atual = "", None
            if t_atual is None:
                t_atual = t
                if troca:
                    atual = "— "
            atual += pedaco if not troca else " " + pedaco.lstrip()
        fim_ant = max(fim, fim_ant or 0.0)
    if atual.strip():
        fechar()
    return pars


def json3_paragrafos(caminho: str):
    with open(caminho, encoding="utf-8") as f:
        j = json.load(f)
    palavras = []
    for ev in j.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        t0 = ev.get("tStartMs", 0) / 1000.0
        primeiro = True
        for s in segs:
            u = (s.get("utf8") or "").replace("\n", " ")
            if not u.strip():
                continue
            if primeiro and not u.startswith(" "):
                u = " " + u
            primeiro = False
            t = t0 + s.get("tOffsetMs", 0) / 1000.0
            palavras.append((t, u, t))
    return paragrafos_legenda(palavras)


def escolher_legenda(arquivos: list[str]) -> str | None:
    def idioma(p):
        partes = os.path.basename(p).split(".")
        return partes[-2] if len(partes) >= 3 else ""
    ordem = ["pt-BR", "pt", "pt-orig", "pt-PT"]
    por_idioma = {idioma(p): p for p in arquivos}
    for lang in ordem:
        if lang in por_idioma:
            return por_idioma[lang]
    return arquivos[0] if arquivos else None


def cmd_legenda(a) -> int:
    try:
        info = info_video(a.ytdlp, a.url)
    except RuntimeError as e:
        erro(str(e))
        return 1
    avisar(f">> {info['titulo']}")
    sessao = a.sessao or pasta_sessao(a.dados, f"{info['id']}-legenda")
    for p in glob.glob(os.path.join(sessao, "legenda.*")):
        os.remove(p)
    cmd = [a.ytdlp, "--no-warnings", "--no-playlist", "--skip-download", "--write-subs",
           "--write-auto-subs", "--sub-langs", "pt.*,pt", "--sub-format", "json3",
           "-o", os.path.join(sessao, "legenda.%(ext)s"), info["url"]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    arquivos = glob.glob(os.path.join(sessao, "legenda.*.json3"))
    if not arquivos:
        if r.returncode != 0:
            erro(explicar_erro_ytdlp(r.stderr))
        else:
            avisar(">> O YouTube não tem legenda em português para este vídeo "
                   "(ainda não gerou, ou o canal desativou). Use: tce gravada LINK")
        return 1
    escolhida = escolher_legenda(arquivos)
    pars = json3_paragrafos(escolhida)
    pasta = os.path.expanduser(a.pasta_saida)
    os.makedirs(pasta, exist_ok=True)
    txt = caminho_livre(os.path.join(pasta, f"{nome_arquivo(info['titulo'])} - LEGENDA YOUTUBE.txt"))
    cab = [f"LEGENDA DO YOUTUBE (rápida, qualidade menor que a versão fiel)", "",
           f"Título: {info['titulo']}", f"Canal:  {info['canal']}", f"Link:   {info['url']}",
           f"Data:   {data_br(info['data'])}",
           f"Gerado em {dt.datetime.now():%d/%m/%Y %H:%M} a partir da legenda "
           f"({os.path.basename(escolhida).split('.')[-2]}) do próprio YouTube.",
           "A legenda automática do YouTube costuma vir sem pontuação; use a versão fiel para citar.",
           LINHA, ""]
    esc = Escritor(txt, cab, None)
    esc.paragrafos(pars)
    esc.linhas(["", LINHA, f"Fim. {len(pars)} parágrafos."])
    esc.fechar()
    avisar_galeria(txt)
    avisar(f"\n>> Pronto! TXT salvo em:\n   {txt}")
    return 0


# ---- teste de velocidade ----------------------------------------------------

def cmd_velocidade(a) -> int:
    with wave.open(a.amostra, "rb") as w:
        pcm = w.readframes(w.getnframes())
        taxa = w.getframerate()
    if taxa != TAXA:
        raise SystemExit("a amostra precisa ser WAV 16 kHz mono")
    while len(pcm) / BPS < 20:          # ~20 s de fala, como um bloco ao vivo
        pcm += pcm
    pcm = pcm[: int(25 * BPS)]
    dur = len(pcm) / BPS
    pasta = os.path.join(a.dados, "teste")
    os.makedirs(pasta, exist_ok=True)
    wav = os.path.join(pasta, "amostra.wav")
    gravar_wav(wav, pcm)
    avisar(f"Teste com {dur:.0f} s de fala, {a.threads} threads:\n")
    for item in a.modelos:
        nome, _, caminho = item.partition("=")
        if not os.path.isfile(caminho):
            continue
        fatores = []
        for extra in (["-bs", "1"], []):          # ao vivo (rápido) e fiel (beam search)
            w = Whisper(a.whisper, caminho, None, a.threads, "en", extra)
            t = time.time()
            try:
                w.transcrever(wav, "", dur)
            except RuntimeError as e:
                avisar(f"  {nome:8s} falhou: {e}")
                break
            fatores.append(dur / (time.time() - t))
        if len(fatores) < 2:
            continue
        rapido, fiel = fatores
        uso = "serve para AO VIVO" if rapido >= 1.6 else "lento para ao vivo"
        avisar(f"  {nome:8s} ao vivo {rapido:4.1f}x ({uso}) · versão fiel {fiel:4.1f}x: "
               f"sessão de 4h em ~{duracao_humana(4 * 3600 / fiel)}")
        print(f"RESULTADO {nome} {rapido:.2f} {fiel:.2f}", flush=True)
    avisar("\n(1x = o tempo da própria sessão; 2x = metade do tempo)")
    return 0


# ---- nuvem (GitHub Actions) -------------------------------------------------

def dividir_partes(ffmpeg: str, ffprobe: str, entrada: str, partes: int,
                   minimo: float = 600.0) -> list[dict]:
    total = duracao_midia(ffprobe, entrada) or 0.0
    partes = max(1, min(int(partes), int(round(total / minimo)) or 1))
    limites = [0.0]
    for i in range(1, partes):
        alvo = total * i / partes
        ini = max(limites[-1] + 60, alvo - 30)
        fonte = FonteFFmpeg(ffmpeg, entrada, ini, ini + 60)
        pcm = fonte.ler(60 * BPS)
        fonte.fechar()
        corte = ini + ponto_de_corte(pcm, 0, len(pcm) / BPS) / TAXA if pcm else alvo
        limites.append(round(corte, 3))
    lista = []
    for i in range(partes):
        lista.append({"parte": i, "inicio": limites[i],
                      "fim": limites[i + 1] if i + 1 < partes else None})
    return lista


def cmd_dividir(a) -> int:
    print(json.dumps(dividir_partes(a.ffmpeg, a.ffprobe, a.entrada, a.partes, a.min_parte)))
    return 0


def cmd_juntar(a) -> int:
    extras = []
    if a.duracao:
        extras.append(f"Duração: {hms(a.duracao)}")
    partes = json.loads(a.partes_json)
    cab = cabecalho(a.tipo, a.titulo, a.canal or "", a.link or "", a.data or "",
                    a.nome_modelo, extras)
    faltando = 0
    os.makedirs(os.path.dirname(os.path.abspath(a.saida)), exist_ok=True)
    with open(a.saida, "w", encoding="utf-8", newline="\n") as out:
        out.write("\n".join(cab) + "\n\n")
        for p in partes:
            frag = os.path.join(a.pasta, f"parte-{int(p['parte']):03d}.txt")
            if os.path.exists(frag):
                with open(frag, encoding="utf-8") as f:
                    out.write(f.read())
            else:
                faltando += 1
                fim = hms(p["fim"]) if p.get("fim") is not None else "o fim"
                out.write(f"[{hms(p['inicio'])}] [ATENÇÃO: o trecho de {hms(p['inicio'])} até {fim} "
                          f"não foi transcrito (falha na nuvem). Rode de novo ou use o celular.]\n")
        out.write("\n" + LINHA + "\n")
        out.write(f"Fim. Transcrição feita na nuvem (GitHub Actions) em {len(partes)} partes em paralelo "
                  f"({dt.datetime.now():%d/%m/%Y %H:%M} UTC).\n")
    if faltando:
        erro(f"{faltando} parte(s) não foram transcritas; o TXT marca os trechos.")
    return 0


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.token = token

    def req(self, metodo: str, url: str, dados=None, cabecalhos=None, bruto=False, timeout=60):
        if not url.startswith("http"):
            url = "https://api.github.com" + url
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "TCE-Transcritor"}
        h.update(cabecalhos or {})
        corpo = dados
        if isinstance(dados, (dict, list)):
            corpo = json.dumps(dados).encode()
            h["Content-Type"] = "application/json"
        pedido = urllib.request.Request(url, data=corpo, method=metodo, headers=h)
        try:
            with urllib.request.urlopen(pedido, timeout=timeout) as r:
                conteudo = r.read()
        except urllib.error.HTTPError as e:
            texto = e.read().decode("utf-8", "replace")[:500]
            if e.code == 401:
                raise RuntimeError("o token do GitHub foi recusado (expirou ou foi digitado errado). "
                                   "Rode: tce nuvem-configurar")
            if e.code == 403 or e.code == 404 and "Not Found" in texto and metodo != "GET":
                raise RuntimeError(f"o token não tem permissão para isso ({e.code}). Ele precisa de "
                                   "'Contents: Read and write' e 'Actions: Read and write'. Rode: tce nuvem-configurar")
            raise RuntimeError(f"GitHub respondeu {e.code}: {texto}")
        if bruto:
            return conteudo
        return json.loads(conteudo) if conteudo else None


class _LeitorProgresso:
    def __init__(self, f, total: int):
        self.f, self.total, self.lido, self.ultimo = f, total, 0, 0.0

    def read(self, n=-1):
        d = self.f.read(65536 if n is None or n < 0 else n)
        self.lido += len(d)
        if time.time() - self.ultimo > 2 or self.lido >= self.total:
            self.ultimo = time.time()
            print(f"\r   enviando: {100 * self.lido / max(1, self.total):5.1f}% "
                  f"({self.lido / 1e6:.1f} de {self.total / 1e6:.1f} MB)", end="", flush=True)
        return d

    def __len__(self):
        return self.total


def ler_token(a) -> str | None:
    token_arq = os.path.expanduser(a.token_arquivo)
    if not os.path.exists(token_arq):
        avisar("Falta configurar o acesso ao GitHub. Rode: tce nuvem-configurar")
        return None
    with open(token_arq, encoding="utf-8") as f:
        return f.read().strip()


def cmd_nuvem_testar(a) -> int:
    token = ler_token(a)
    if not token:
        return 1
    gh = GitHub(a.repo, token)
    try:
        repo = gh.req("GET", f"/repos/{a.repo}")
        gh.req("GET", f"/repos/{a.repo}/actions/workflows/transcrever.yml")
    except RuntimeError as e:
        erro(str(e))
        if "404" in str(e):
            avisar("O repositório ou o arquivo .github/workflows/transcrever.yml não foi encontrado "
                   f"no ramo principal de {a.repo}.")
        return 1
    avisar(f"Acesso OK ao repositório {repo['full_name']} (ramo {repo['default_branch']}).")
    if repo.get("private"):
        avisar("Atenção: repositório privado usa os minutos gratuitos do GitHub (2.000/mês). "
               "Em repositório público é ilimitado.")
    else:
        avisar("Repositório público: a nuvem é gratuita e sem limite de minutos, "
               "mas os TXT ficam visíveis para quem abrir o repositório.")
    return 0


def cmd_nuvem(a) -> int:
    token = ler_token(a)
    if not token:
        return 1
    gh = GitHub(a.repo, token)
    alvo = a.entrada
    titulo, link, canal, data = a.titulo, a.link or "", "", ""
    try:
        if re.match(r"^(https?://|www\.|youtu)", alvo):
            info = info_video(a.ytdlp, alvo)
            avisar(f">> {info['titulo']}")
            if info["status"] in ("is_live", "is_upcoming"):
                avisar(">> Esta live ainda está no ar. Use AO VIVO agora e mande para a nuvem quando acabar "
                       "(tce nuvem fiel).")
                return 3
            pasta_audio = pasta_sessao(a.dados, f"{info['id']}-nuvem")
            entrada = baixar_audio(a, info["url"], pasta_audio)
            titulo = titulo or info["titulo"]
            link, canal, data = info["url"], info["canal"], data_br(info["data"])
        elif os.path.isdir(os.path.expanduser(alvo)):
            pasta_live = os.path.abspath(os.path.expanduser(alvo))
            est = ler_json(os.path.join(pasta_live, "estado.json"), {}) or {}
            entrada = os.path.join(pasta_live, "audio16k.pcm")
            if not os.path.exists(entrada):
                raise RuntimeError(f"não achei o áudio da live em {pasta_live}")
            titulo = titulo or est.get("titulo") or os.path.basename(pasta_live)
            link, canal, data = est.get("url", ""), est.get("canal", ""), data_br(est.get("data"))
        else:
            entrada = os.path.abspath(os.path.expanduser(alvo))
            if not os.path.isfile(entrada):
                raise RuntimeError(f"arquivo não encontrado: {entrada}")
            titulo = titulo or os.path.splitext(os.path.basename(entrada))[0]
    except RuntimeError as e:
        erro(str(e))
        return 1

    ident = time.strftime("%Y%m%d%H%M%S") + hashlib.sha1(entrada.encode()).hexdigest()[:4]
    pasta = pasta_sessao(a.dados, f"envio-{ident}")
    try:
        repo = gh.req("GET", f"/repos/{a.repo}")
        ramo = repo["default_branch"]

        avisar(">> 1/4 Compactando o áudio para enviar...")
        opus = os.path.join(pasta, f"{ident}.ogg")
        cmd = [a.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        if entrada.endswith(".pcm"):
            cmd += ["-f", "s16le", "-ar", str(TAXA), "-ac", "1"]
        cmd += ["-i", entrada, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus",
                "-b:a", "32k", "-application", "voip", opus]
        if subprocess.run(cmd).returncode != 0:
            raise RuntimeError("não consegui compactar o áudio com o ffmpeg")
        tamanho = os.path.getsize(opus)

        avisar(f">> 2/4 Enviando {tamanho / 1e6:.0f} MB para o GitHub...")
        try:
            rel = gh.req("GET", f"/repos/{a.repo}/releases/tags/audios")
        except RuntimeError:
            rel = gh.req("POST", f"/repos/{a.repo}/releases", {
                "tag_name": "audios", "target_commitish": ramo, "prerelease": True,
                "name": "Áudios temporários (TCE Transcritor)",
                "body": "Áudios enviados pelo comando `tce nuvem` para transcrição. "
                        "Cada um é apagado automaticamente quando a transcrição termina."})
        with open(opus, "rb") as f:
            asset = gh.req("POST", f"https://uploads.github.com/repos/{a.repo}/releases/{rel['id']}"
                           f"/assets?name={urllib.parse.quote(os.path.basename(opus))}",
                           _LeitorProgresso(f, tamanho),
                           {"Content-Type": "application/ogg", "Content-Length": str(tamanho)},
                           timeout=900)
        print()

        avisar(">> 3/4 Pedindo a transcrição na nuvem...")
        nome_txt = f"{nome_arquivo(titulo)} - {a.sufixo}.txt"
        gh.req("POST", f"/repos/{a.repo}/actions/workflows/transcrever.yml/dispatches", {
            "ref": ramo, "inputs": {
                "audio": asset["browser_download_url"], "titulo": titulo[:200],
                "arquivo": nome_txt, "modelo": a.modelo_nuvem, "partes": str(a.partes),
                "link": link[:300], "canal": canal[:100], "data": data, "id": ident}})
        run = None
        for _ in range(50):
            time.sleep(6)
            runs = gh.req("GET", f"/repos/{a.repo}/actions/workflows/transcrever.yml/runs"
                                 f"?event=workflow_dispatch&per_page=20")
            for r_ in runs.get("workflow_runs", []):
                if ident in (r_.get("display_title") or ""):
                    run = r_
                    break
            if run:
                break
        if not run:
            raise RuntimeError("a nuvem não iniciou a transcrição (veja a aba Actions do repositório)")
        avisar(f"   Acompanhe também pelo navegador: {run['html_url']}")

        avisar(">> 4/4 Transcrevendo na nuvem (pode deixar o celular de lado)...")
        inicio = time.time()
        while True:
            time.sleep(30)
            try:
                r_ = gh.req("GET", f"/repos/{a.repo}/actions/runs/{run['id']}")
            except RuntimeError as e:   # internet do celular oscilou: tenta de novo
                avisar(f"   (sem resposta do GitHub: {e}; tentando de novo)")
                continue
            try:
                jobs = gh.req("GET", f"/repos/{a.repo}/actions/runs/{run['id']}/jobs?per_page=50")["jobs"]
                partes = [j for j in jobs if j["name"].startswith("transcrever")]
                feitas = sum(1 for j in partes if j["status"] == "completed")
                detalhe = f"partes prontas: {feitas} de {len(partes)}" if partes else "preparando o áudio"
            except Exception:
                detalhe = r_.get("status", "")
            avisar(f"   {duracao_humana(time.time() - inicio)} · {detalhe}")
            notificar(a.notificar, "TCE: transcrevendo na nuvem", detalhe)
            if r_["status"] == "completed":
                break
        try:
            conteudo = gh.req("GET", f"/repos/{a.repo}/contents/transcricoes/"
                                     f"{urllib.parse.quote(nome_txt)}?ref={ramo}",
                              cabecalhos={"Accept": "application/vnd.github.raw"},
                              bruto=True, timeout=180)
        except RuntimeError:
            conteudo = None
        if conteudo is None:
            raise RuntimeError(f"a transcrição na nuvem falhou ({r_['conclusion']}). "
                               f"Detalhes: {r_['html_url']}")
        if r_["conclusion"] != "success":
            avisar(">> ATENÇÃO: parte da transcrição falhou; os trechos faltantes estão marcados no TXT.")
        destino_pasta = os.path.expanduser(a.pasta_saida)
        os.makedirs(destino_pasta, exist_ok=True)
        destino = caminho_livre(os.path.join(destino_pasta, nome_txt))
        with open(destino, "wb") as f:
            f.write(conteudo)
        avisar_galeria(destino)
        notificar(a.notificar, "TCE Transcritor: pronto", nome_txt, fixa=False)
        avisar(f"\n>> Pronto! TXT salvo em:\n   {destino}")
        shutil.rmtree(pasta, ignore_errors=True)
        return 0
    except RuntimeError as e:
        erro(str(e))
        return 1


# ----------------------------------------------------------------------------
# linha de comando
# ----------------------------------------------------------------------------

def threads_padrao() -> int:
    try:
        return max(1, min(4, os.cpu_count() or 4))
    except Exception:
        return 4


def opcoes_comuns(p: argparse.ArgumentParser) -> None:
    dados = os.environ.get("TCE_DADOS", os.path.expanduser("~/.local/share/tce"))
    p.add_argument("--whisper", default=os.environ.get("TCE_WHISPER", "whisper-cli"))
    p.add_argument("--modelo", default=os.environ.get("TCE_MODELO", ""))
    p.add_argument("--nome-modelo", default=os.environ.get("TCE_NOME_MODELO", ""))
    p.add_argument("--vad-modelo", default=os.environ.get("TCE_VAD", ""))
    p.add_argument("--threads", type=int, default=int(os.environ.get("TCE_THREADS", threads_padrao())))
    p.add_argument("--idioma", default=os.environ.get("TCE_IDIOMA", "pt"))
    p.add_argument("--prompt", default=os.environ.get("TCE_PROMPT", PROMPT_PADRAO))
    p.add_argument("--whisper-extra", default=os.environ.get("TCE_WHISPER_EXTRA", ""))
    p.add_argument("--bloco", type=float, default=None,
                   help="segundos por bloco (ao vivo ~25, arquivos ~300)")
    p.add_argument("--pasta-saida", default=os.environ.get("TCE_SAIDA", os.path.expanduser("~/TCE-Transcricoes")))
    p.add_argument("--dados", default=dados)
    p.add_argument("--sessao", default=None)
    p.add_argument("--ffmpeg", default=os.environ.get("TCE_FFMPEG", "ffmpeg"))
    p.add_argument("--ffprobe", default=os.environ.get("TCE_FFPROBE", "ffprobe"))
    p.add_argument("--ytdlp", default=os.environ.get("TCE_YTDLP", "yt-dlp"))
    p.add_argument("--formato-aovivo", default=os.environ.get("TCE_FORMATO_AOVIVO", "ba/91/92/93/94/w"))
    p.add_argument("--formato-gravada", default=os.environ.get("TCE_FORMATO_GRAVADA", "ba[abr<=?96]/ba/b"))
    p.add_argument("--notificar", action="store_true", default=os.environ.get("TCE_NOTIFICAR") == "1")
    p.add_argument("--refazer", action="store_true", help="ignora o que já foi feito e começa do zero")
    p.add_argument("--manter-audio", action="store_true", default=os.environ.get("TCE_MANTER_AUDIO") == "1")
    p.add_argument("--guardar-audio", action="store_true", default=os.environ.get("TCE_GUARDAR_AUDIO", "1") == "1")
    p.add_argument("--tipo", default=None)
    p.add_argument("--sufixo", default=None)


def principal(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="motor.py", description="TCE Transcritor - motor")
    ap.add_argument("--versao", action="version", version=VERSAO)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="mostra título e situação de um link")
    p.add_argument("url")
    p.add_argument("--ytdlp", default=os.environ.get("TCE_YTDLP", "yt-dlp"))

    p = sub.add_parser("aovivo", help="transcreve uma live em tempo real")
    p.add_argument("url")
    p.add_argument("--forcar", action="store_true")
    opcoes_comuns(p)

    p = sub.add_parser("gravada", help="baixa o áudio de um vídeo e faz a transcrição completa")
    p.add_argument("url")
    opcoes_comuns(p)

    p = sub.add_parser("arquivo", help="transcreve um arquivo de áudio/vídeo do celular")
    p.add_argument("entrada")
    p.add_argument("--titulo", default=None)
    p.add_argument("--link", default=None)
    p.add_argument("--data", default=None)
    p.add_argument("--inicio", type=float, default=None)
    p.add_argument("--fim", type=float, default=None)
    p.add_argument("--fragmento", default=None, help="(nuvem) grava só os parágrafos neste arquivo")
    p.add_argument("--silencioso", action="store_true")
    opcoes_comuns(p)

    p = sub.add_parser("fiel", help="refaz uma live gravada pelo celular com o modelo grande")
    p.add_argument("sessao_aovivo", nargs="?", default=None)
    opcoes_comuns(p)

    p = sub.add_parser("legenda", help="baixa a legenda automática do YouTube como TXT")
    p.add_argument("url")
    opcoes_comuns(p)

    p = sub.add_parser("velocidade", help="mede a velocidade dos modelos neste aparelho")
    p.add_argument("--amostra", required=True)
    p.add_argument("modelos", nargs="+", help="nome=caminho")
    opcoes_comuns(p)

    p = sub.add_parser("dividir", help="(nuvem) acha pontos de corte para dividir o áudio")
    p.add_argument("entrada")
    p.add_argument("--partes", type=int, default=8)
    p.add_argument("--min-parte", type=float, default=600.0, help="segundos mínimos por parte")
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--ffprobe", default="ffprobe")

    p = sub.add_parser("juntar", help="(nuvem) junta os pedaços num TXT só")
    p.add_argument("--pasta", required=True, help="pasta com parte-000.txt, parte-001.txt...")
    p.add_argument("--partes-json", required=True)
    p.add_argument("--saida", required=True)
    p.add_argument("--titulo", required=True)
    p.add_argument("--tipo", default="TRANSCRIÇÃO NA NUVEM (versão fiel)")
    p.add_argument("--canal", default="")
    p.add_argument("--link", default="")
    p.add_argument("--data", default="")
    p.add_argument("--nome-modelo", default="")
    p.add_argument("--duracao", type=float, default=None)

    p = sub.add_parser("nuvem-testar", help="confere o acesso ao GitHub")
    p.add_argument("--repo", default=os.environ.get("TCE_REPO", "FaithfulSnake/TCETranscritor"))
    p.add_argument("--token-arquivo", default=os.environ.get("TCE_TOKEN", "~/.config/tce/token"))

    p = sub.add_parser("nuvem", help="transcreve no GitHub Actions (link, arquivo ou live gravada)")
    p.add_argument("entrada", help="link do YouTube, arquivo de áudio ou pasta de uma live gravada")
    p.add_argument("--titulo", default=None)
    p.add_argument("--link", default=None)
    p.add_argument("--repo", default=os.environ.get("TCE_REPO", "FaithfulSnake/TCETranscritor"))
    p.add_argument("--token-arquivo", default=os.environ.get("TCE_TOKEN", "~/.config/tce/token"))
    p.add_argument("--modelo-nuvem", default=os.environ.get("TCE_MODELO_NUVEM", "turbo"))
    p.add_argument("--partes", type=int, default=int(os.environ.get("TCE_PARTES_NUVEM", "16")))
    opcoes_comuns(p)

    a = ap.parse_args(argv)
    if getattr(a, "bloco", 0) is None:
        a.bloco = 25.0 if a.cmd == "aovivo" else 300.0
    if hasattr(a, "nome_modelo") and not a.nome_modelo and getattr(a, "modelo", ""):
        a.nome_modelo = os.path.basename(a.modelo).replace("ggml-", "").replace(".bin", "")
    if a.cmd in ("aovivo", "gravada", "arquivo", "fiel") and not a.modelo:
        raise SystemExit("Informe o modelo (--modelo) - normalmente o comando 'tce' faz isso.")
    if hasattr(a, "sufixo") and a.cmd == "nuvem" and not a.sufixo:
        a.sufixo = "FIEL (nuvem)"
    funcoes = {"info": cmd_info, "aovivo": cmd_aovivo, "gravada": cmd_gravada,
               "arquivo": cmd_arquivo, "fiel": cmd_fiel, "legenda": cmd_legenda,
               "velocidade": cmd_velocidade, "dividir": cmd_dividir, "juntar": cmd_juntar,
               "nuvem": cmd_nuvem, "nuvem-testar": cmd_nuvem_testar}
    return funcoes[a.cmd](a)


def _sigterm(sig, frame):
    """O Android/`kill` pedem para encerrar: trata como Ctrl+C (o progresso já está salvo)."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        sys.exit(principal())
    except KeyboardInterrupt:
        avisar("\n>> Interrompido.")
        sys.exit(130)
