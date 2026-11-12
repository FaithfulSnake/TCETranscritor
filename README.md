# TCE Transcritor

Transcrição **integral** de lives e vídeos do YouTube (por exemplo, as sessões do
TCE-MA no canal [TCEMA.OFICIAL](https://www.youtube.com/@TCEMAOFICIAL)) **direto no
celular Android**, com o TXT salvo na pasta **Download**. Tudo grátis e de código aberto,
sem assinatura e sem PC.

| O que você quer | O que usar | Resultado |
|---|---|---|
| Acompanhar uma live **agora** | **AO VIVO** | texto aparece na hora (a cada ~25 s) e vai sendo salvo no TXT |
| Transcrição integral de uma sessão gravada | **FIEL** (no celular) | TXT com o modelo grande (Whisper large-v3-turbo) |
| A mesma coisa, rápido e sem esquentar o celular | **NUVEM** (GitHub Actions) | TXT pronto em ~30 min, grátis |
| Só uma ideia rápida do que foi dito | **LEGENDA** | legenda automática do próprio YouTube em segundos |
| Um áudio/vídeo que já está no celular | **ARQUIVO** | TXT do arquivo |

O áudio vem **direto do YouTube** (não é gravado pelo alto-falante): dá para apagar a
tela, usar o WhatsApp e ouvir outras coisas sem atrapalhar a transcrição.

## Instalação (uma vez, uns 30 minutos, use Wi-Fi)

1. Instale o **Termux** pelo F-Droid: <https://f-droid.org/packages/com.termux/>
   (ou pelo GitHub: <https://github.com/termux/termux-app/releases>, arquivo
   `...arm64-v8a.apk`). **Não use a versão da Play Store.**
2. Abra o Termux, cole a linha abaixo e tecle Enter:

   ```
   curl -fsSL https://raw.githubusercontent.com/FaithfulSnake/TCETranscritor/HEAD/instalar.sh | bash
   ```

3. Quando o Android pedir acesso aos arquivos, toque em **Permitir**.
4. Espere aparecer **Tudo pronto!** O instalador baixa o yt-dlp, compila o whisper.cpp
   no próprio celular, baixa os modelos (~850 MB), mede a velocidade do aparelho e
   testa o acesso ao YouTube. Precisa de uns **3 GB livres**.

Opcionais (também do F-Droid): **Termux:API** (notificação com o progresso; depois
ponha `NOTIFICAR=1` na configuração) e **Termux:Widget** (atalho na tela inicial).

Antes de sessões longas: Configurações do Android > Apps > Termux > Bateria >
**Sem restrições**, e deixe o celular na tomada.

## Como usar

### Live acontecendo agora
No app do YouTube, abra a live > **Compartilhar** > **Termux** > opção **1 (AO VIVO)**.

- O texto aparece na tela e vai para `Download/TCE-Transcricoes/<título> - AO VIVO.txt`
  a cada bloco. Se o celular desligar no meio, o que já saiu está salvo.
- Pode iniciar antes da sessão começar: ele espera a live entrar no ar.
- Se a internet cair, ele reconecta sozinho e marca no TXT onde pode faltar um trecho.
- Para parar: **Ctrl+C** (tecla CTRL na barra de cima do Termux, depois C). Ele termina
  de transcrever o que já foi gravado.
- Quando a live acaba, ele pergunta se você quer a **versão FIEL** (no celular ou na nuvem).

### Sessão gravada (live que já terminou ou qualquer vídeo)
Compartilhar > Termux > **2 (FIEL)** no celular, ou **3 (NUVEM)** para ficar pronto mais rápido.

O resultado fica em `Download/TCE-Transcricoes/<título> - FIEL.txt`, assim:

```
TRANSCRIÇÃO AUTOMÁTICA - VÍDEO GRAVADO (versão fiel)

Título: 32ª SESSÃO ORDINÁRIA DO PLENO - 04/11/2026
Canal:  TCEMA.OFICIAL
Link:   https://www.youtube.com/watch?v=...
...
========================================================================

[00:00:12] Bom dia a todos. Havendo número legal, declaro aberta a sessão...
[00:01:05] Passamos à apreciação do processo...
```

### Comandos (se preferir digitar)

| Comando | Faz |
|---|---|
| `tce` | menu |
| `tce aovivo LINK` | live em tempo real |
| `tce gravada LINK` | transcrição integral fiel no celular |
| `tce rapida LINK` | integral com o modelo rápido (bem mais veloz, menos preciso) |
| `tce nuvem LINK` | integral na nuvem do GitHub |
| `tce nuvem fiel` | manda a última live gravada para a nuvem |
| `tce fiel` | versão fiel da última live gravada, no celular |
| `tce legenda LINK` | legenda do YouTube em TXT |
| `tce arquivo` | vídeo ou áudio que já está no celular: mostra os mais recentes para escolher pelo número (ou: arquivo > Compartilhar > Termux > EDIT) |
| `tce velocidade` | mede quanto tempo cada modelo leva neste celular |
| `tce atualizar` | atualiza (rode se o YouTube começar a dar erro) |
| `tce ajuda` | todos os comandos |

Interrompeu no meio (Ctrl+C, bateria, Android fechou)? Rode **o mesmo comando de
novo**: ele continua de onde parou, sem repetir texto.

## Quanto tempo leva

Depende do celular; rode `tce velocidade` para ver os números do seu. Referência:

- **LEGENDA**: segundos. As sessões do canal do TCE-MA têm legenda automática em
  português (com pontuação razoável); é o jeito mais rápido de ter o texto inteiro.
- **AO VIVO** (modelo `small`): no teste automático dentro do Termux (processador ARM
  de 4 núcleos) rodou a 3x o tempo real, ou seja, sobra folga para acompanhar a live.
  Se o seu celular não der conta, o instalador troca sozinho para o `base`.
- **FIEL no celular** (modelo `turbo`): bem mais pesado que o `small`; em celular
  intermediário, algo entre 1x e 3x a duração da sessão (4 h de sessão podem levar a
  noite). Deixe na tomada, ou use a **NUVEM**, ou a opção **RÁPIDA** (`small`, que no
  teste ficou bom em português e roda ~2,5x mais rápido que o tempo real).
- **NUVEM**: a sessão é dividida em até 16 partes transcritas ao mesmo tempo com o
  modelo grande. No teste, 30 min de áudio em 3 partes ficaram prontos em 22 min
  (já contando a primeira compilação); cada máquina faz ~0,6x o tempo real, então
  4 h de sessão em 16 partes devem ficar prontas em cerca de 30 minutos.

## Nuvem do GitHub (opcional, uma vez só)

O YouTube bloqueia downloads feitos pelos servidores do GitHub, então o celular baixa
o áudio (leve, ~60 MB para 4 h), manda para o GitHub, as máquinas do GitHub Actions
transcrevem e o TXT volta sozinho para a pasta Download.

1. Pelo navegador do celular, crie um token em
   <https://github.com/settings/personal-access-tokens/new>:
   - *Repository access*: **Only select repositories** > `TCETranscritor`
   - *Permissions*: **Actions: Read and write** e **Contents: Read and write**
2. No Termux: `tce nuvem-configurar` e cole o token.

Grátis e sem limite de minutos porque o repositório é **público**, o que também
significa que os TXT gerados pela nuvem (pasta `transcricoes/`) ficam visíveis
para quem abrir o repositório. As sessões do TCE já são públicas. Para algo
sigiloso, use a transcrição no celular, que não sai do aparelho.

Também dá para rodar pela aba **Actions** > **Transcrever na nuvem** > *Run workflow*,
colando um link direto de arquivo de áudio.

## Limites reais

- É transcrição automática: pode errar nomes próprios, números de processo e termos
  raros. **Confira no vídeo os trechos que for citar.** Não identifica quem está
  falando. Dá para melhorar nomes colocando-os em `PROMPT=` na configuração.
- Se você começar o AO VIVO depois que a sessão já começou, o início fica de fora.
  Para a sessão inteira, use `tce gravada LINK` depois que a live terminar.
- Logo depois que uma live termina, o YouTube passa algumas horas processando o vídeo
  e o download pode vir incompleto. Nesse intervalo, use `tce fiel` (a gravação feita
  pelo celular) ou espere.
- O YouTube muda com frequência; se começar a dar erro, `tce atualizar`.

## Solução de problemas

| Problema | Solução |
|---|---|
| `Sign in to confirm you're not a bot` | `tce atualizar`; tente de novo depois de alguns minutos ou troque Wi-Fi/dados móveis |
| `Illegal instruction` ou erro estranho do whisper | `tce reparar` |
| `[Process completed (signal 9)]` ou o Termux fechou sozinho | bateria **sem restrições** para o Termux e não feche a sessão do Termux; no Android 14+, Opções do desenvolvedor > *Desativar restrições de processos filhos*. Depois, rode o mesmo comando: ele continua de onde parou |
| Não acho o TXT | `tce pasta` mostra onde está (normalmente Download/TCE-Transcricoes) |
| Qualquer outra coisa | `tce diagnostico` confere yt-dlp, YouTube, whisper e modelos (e se ajusta sozinho quando dá) |
| Celular fraco demais para o AO VIVO | na configuração (`~/.config/tce/config`) ponha `MODELO_AOVIVO="base"` |

## Como funciona

`yt-dlp` (com `deno` para os desafios do YouTube) puxa só o áudio → `ffmpeg` converte
para 16 kHz mono → o áudio é cortado em blocos **nos pontos de silêncio** (não parte
palavras) → [whisper.cpp](https://github.com/ggml-org/whisper.cpp) v1.9.5 com detector
de voz Silero (evita texto inventado no silêncio) → o TXT é gravado e sincronizado no
disco a cada bloco, com um arquivo de estado para retomar.

Peças: Termux (GPLv3), yt-dlp (Unlicense), ffmpeg (LGPL/GPL), whisper.cpp e modelos
Whisper (MIT). O código deste repositório está em `tce/motor.py` (motor), `tce/tce`
(comando e menu) e `instalar.sh`; os testes rodam a cada mudança em
`.github/workflows/teste.yml`, inclusive dentro de um Termux aarch64.
