# Instalador do TCE Transcritor para Windows - NÃO precisa de administrador.
#
# No cmd (Prompt de Comando), cole e tecle Enter:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=3072; irm https://raw.githubusercontent.com/FaithfulSnake/TCETranscritor/HEAD/instalar.ps1 | iex"
#
# Tudo fica em %LOCALAPPDATA%\TCETranscritor (Python portátil, yt-dlp, deno, ffmpeg,
# whisper.cpp e modelos). Pode rodar de novo quando quiser: ele só baixa o que faltar
# e atualiza o yt-dlp e os scripts.
# Variáveis opcionais: TCE_MODELOS="small turbo", TCE_FONTE=pasta local (testes), TCE_TESTE=1.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch {}

$Base     = Join-Path $env:LOCALAPPDATA 'TCETranscritor'
$Bin      = Join-Path $Base 'bin'
$Whisper  = Join-Path $Base 'whisper'
$Modelos  = Join-Path $Base 'modelos'
$Amostras = Join-Path $Base 'amostras'
$App      = Join-Path $Base 'app'
$Py       = Join-Path $Base 'py'
$Tmp      = Join-Path $Base 'tmp'
$WhisperBuild = 'b5454'   # binários do whisper.cpp v1.9.5
$PyVersoes = @('3.14.0', '3.13.1', '3.12.8')
$ModelosPedidos = if ($env:TCE_MODELOS) { $env:TCE_MODELOS -split '\s+' } else { @('small', 'turbo') }
$ArqModelo = @{ tiny='ggml-tiny-q8_0.bin'; base='ggml-base-q8_0.bin'; small='ggml-small-q8_0.bin';
                medium='ggml-medium-q5_0.bin'; turbo='ggml-large-v3-turbo-q5_0.bin';
                'turbo-q8'='ggml-large-v3-turbo-q8_0.bin'; large='ggml-large-v3-q5_0.bin' }

function Passo($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok($t)    { Write-Host "    ok: $t" -ForegroundColor Green }
function Aviso($t) { Write-Host "    atenção: $t" -ForegroundColor Yellow }

function Baixar([string]$Url, [string]$Destino) {
    if ((Test-Path $Destino) -and ((Get-Item $Destino).Length -gt 0)) { return }
    New-Item -ItemType Directory -Force -Path (Split-Path $Destino) | Out-Null
    $parcial = "$Destino.part"
    Write-Host "    baixando $(Split-Path $Destino -Leaf)..."
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    for ($i = 1; $i -le 5; $i++) {
        try {
            if (Test-Path $curl) {
                & $curl -fL --retry 3 --retry-delay 3 -C - --progress-bar -o $parcial $Url
                if ($LASTEXITCODE -ne 0) { throw "curl saiu com código $LASTEXITCODE" }
            } else {
                Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $parcial -ErrorAction Stop
            }
            Move-Item -Force $parcial $Destino
            return
        } catch {
            Write-Host "    falhou ($($_.Exception.Message)); tentando de novo ($i/5)..."
            Start-Sleep -Seconds (3 * $i)
        }
    }
    throw "não consegui baixar $Url"
}

function Nativo([string]$Exe, [string[]]$Argumentos) {
    # roda um programa e devolve {Codigo, Saida}; no PowerShell 5.1 o stderr de um programa
    # com 2>&1 vira erro fatal se $ErrorActionPreference for 'Stop', então aqui é 'Continue'
    $ErrorActionPreference = 'Continue'
    $saida = & $Exe @Argumentos 2>&1 | ForEach-Object { "$_" } | Out-String
    return @{ Codigo = $LASTEXITCODE; Saida = $saida }
}

function Extrair([string]$Zip, [string]$Destino) {
    if (Test-Path $Destino) { Remove-Item -Recurse -Force $Destino }
    New-Item -ItemType Directory -Force -Path $Destino | Out-Null
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (Test-Path $tar) {
        & $tar -xf $Zip -C $Destino
        if ($LASTEXITCODE -eq 0) { return }
    }
    Expand-Archive -Force -Path $Zip -DestinationPath $Destino -ErrorAction Stop
}

try {
    Write-Host "TCE Transcritor - instalação no Windows (sem administrador)" -ForegroundColor Magenta
    foreach ($d in @($Base, $Bin, $Whisper, $Modelos, $Amostras, $App, $Tmp)) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
    $arq64 = ($env:PROCESSOR_ARCHITECTURE -eq 'AMD64') -or ($env:PROCESSOR_ARCHITEW6432 -eq 'AMD64')
    if (-not $arq64) { Aviso "este computador não é x64 ($env:PROCESSOR_ARCHITECTURE); vou usar os programas x64 (no Windows 11 ARM eles rodam emulados)" }

    Passo "Python portátil"
    if (-not (Test-Path (Join-Path $Py 'python.exe'))) {
        $feito = $false
        foreach ($v in $PyVersoes) {
            try {
                $zip = Join-Path $Tmp "python-$v-embed-amd64.zip"
                Baixar "https://www.python.org/ftp/python/$v/python-$v-embed-amd64.zip" $zip
                Extrair $zip $Py
                $feito = $true
                break
            } catch { Aviso "Python $v não baixou; tentando outra versão" }
        }
        if (-not $feito) { throw "não consegui baixar o Python portátil" }
    }
    Ok (Nativo (Join-Path $Py 'python.exe') @('--version')).Saida.Trim()

    Passo "yt-dlp e deno (pegam o áudio direto do YouTube)"
    $ytdlp = Join-Path $Bin 'yt-dlp.exe'
    if (Test-Path $ytdlp) {
        $r = Nativo $ytdlp @('-U')
        Write-Host ("    " + (($r.Saida.Trim() -split "`n") | Select-Object -Last 1))
    } else {
        Baixar 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' $ytdlp
    }
    if (-not (Test-Path (Join-Path $Bin 'deno.exe'))) {
        $zip = Join-Path $Tmp 'deno.zip'
        Baixar 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' $zip
        Extrair $zip (Join-Path $Tmp 'deno')
        Copy-Item -Force (Join-Path $Tmp 'deno\deno.exe') $Bin
    }
    Ok ("yt-dlp " + (Nativo $ytdlp @('--version')).Saida.Trim())

    Passo "ffmpeg (converte o áudio)"
    if (-not (Test-Path (Join-Path $Bin 'ffmpeg.exe'))) {
        $zip = Join-Path $Tmp 'ffmpeg.zip'
        Baixar 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' $zip
        Extrair $zip (Join-Path $Tmp 'ffmpeg')
        Get-ChildItem -Recurse (Join-Path $Tmp 'ffmpeg') -Include 'ffmpeg.exe', 'ffprobe.exe' |
            ForEach-Object { Copy-Item -Force $_.FullName $Bin }
    }
    Ok "ffmpeg"

    Passo "whisper.cpp (o programa que transcreve)"
    if (-not (Test-Path (Join-Path $Whisper 'whisper-cli.exe'))) {
        $zip = Join-Path $Tmp 'whisper.zip'
        Baixar "https://github.com/ggml-org/whisper.cpp/releases/download/$WhisperBuild/whisper-bin-x64.zip" $zip
        Extrair $zip (Join-Path $Tmp 'whisper')
        $cli = Get-ChildItem -Recurse (Join-Path $Tmp 'whisper') -Filter 'whisper-cli.exe' | Select-Object -First 1
        if (-not $cli) { throw "whisper-cli.exe não veio no pacote do whisper.cpp" }
        Copy-Item -Force (Join-Path $cli.DirectoryName '*') $Whisper
    }
    Baixar 'https://raw.githubusercontent.com/ggml-org/whisper.cpp/v1.9.5/samples/jfk.wav' (Join-Path $Amostras 'jfk.wav')
    Ok "whisper-cli"

    Passo "Scripts do TCE Transcritor"
    $destTce = Join-Path $App 'tce'
    if ($env:TCE_FONTE) {
        $origem = Join-Path $env:TCE_FONTE 'tce'
    } else {
        $zip = Join-Path $Tmp 'app.zip'
        if (Test-Path $zip) { Remove-Item -Force $zip }
        Baixar 'https://github.com/FaithfulSnake/TCETranscritor/archive/HEAD.zip' $zip
        Extrair $zip (Join-Path $Tmp 'app')
        $origem = (Get-ChildItem -Recurse -Directory (Join-Path $Tmp 'app') -Filter 'tce' | Select-Object -First 1).FullName
    }
    if (Test-Path $destTce) { Remove-Item -Recurse -Force $destTce }
    Copy-Item -Recurse -Force $origem $destTce
    Ok $destTce

    Passo "Comando 'tce' e atalho na área de trabalho"
    $cmd = Join-Path $Base 'tce.cmd'
    Set-Content -Encoding Ascii -Path $cmd -Value @(
        '@echo off',
        'chcp 65001 >nul',
        '"%~dp0py\python.exe" "%~dp0app\tce\tce_windows.py" %*'
    )
    $caminhoUsuario = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $caminhoUsuario) { $caminhoUsuario = '' }
    if (($caminhoUsuario -split ';') -notcontains $Base) {
        [Environment]::SetEnvironmentVariable('Path', (($caminhoUsuario.TrimEnd(';') + ";$Base").TrimStart(';')), 'User')
        Ok "comando 'tce' adicionado (abra um cmd NOVO para usar)"
    } else { Ok "comando 'tce'" }
    $env:Path = "$Base;$env:Path"
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $shell = New-Object -ComObject WScript.Shell
        $lnk = $shell.CreateShortcut((Join-Path $desktop 'TCE Transcritor.lnk'))
        $lnk.TargetPath = Join-Path $env:SystemRoot 'System32\cmd.exe'
        $lnk.Arguments = "/k `"$cmd`""
        $lnk.WorkingDirectory = $env:USERPROFILE
        $lnk.Save()
        Ok "atalho 'TCE Transcritor' na área de trabalho"
    } catch { Aviso "não consegui criar o atalho na área de trabalho (use o comando tce)" }
    $config = Join-Path $Base 'config.ini'
    if (-not (Test-Path $config)) {
        Set-Content -Encoding UTF8 -Path $config -Value @(
            '# Configuração do TCE Transcritor (tire o # do começo da linha para mudar)',
            '#PASTA_SAIDA=C:\Users\voce\Downloads\TCE-Transcricoes',
            'MODELO_AOVIVO=small',
            'MODELO_FIEL=turbo',
            '#THREADS=4',
            'FIEL_DEPOIS_DA_LIVE=perguntar',
            'MODELO_NUVEM=turbo',
            'PARTES_NUVEM=16',
            '#PROMPT=Sessão do Tribunal de Contas do Estado do Maranhão. Conselheiro Fulano.'
        )
    }

    Passo "Modelos de transcrição (~850 MB na primeira vez)"
    foreach ($m in $ModelosPedidos) {
        Baixar "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$($ArqModelo[$m])" (Join-Path $Modelos $ArqModelo[$m])
    }
    Baixar 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin' (Join-Path $Modelos 'ggml-silero-v6.2.0.bin')
    Ok "modelos"

    Passo "Teste do whisper.cpp"
    $modeloTeste = Get-ChildItem $Modelos -Filter 'ggml-*.bin' | Where-Object { $_.Name -notlike '*silero*' } |
        Sort-Object Length | Select-Object -First 1
    $r = Nativo (Join-Path $Whisper 'whisper-cli.exe') @('-m', $modeloTeste.FullName, '-f', (Join-Path $Amostras 'jfk.wav'), '-l', 'en', '-nt', '-np')
    $saida = $r.Saida
    if ($r.Codigo -eq -1073741515) {
        throw "falta uma biblioteca do Windows para o whisper.cpp (Microsoft Visual C++ Redistributable). Peça para instalarem o 'VC++ Redistributable x64' ou use o celular/nuvem."
    }
    if ($saida -notmatch 'country') { Write-Host $saida; throw "o teste do whisper.cpp falhou" }
    Ok "o whisper.cpp transcreveu a amostra"

    Passo "Velocidade deste computador"
    $vel = (Nativo $cmd @('velocidade')).Saida
    $vel -split "`n" | Where-Object { $_ -match 'ao vivo .*fiel' } | ForEach-Object { Write-Host $_ }
    if ($vel -match 'RESULTADO small ([0-9.]+)') {
        if ([double]::Parse($Matches[1], [Globalization.CultureInfo]::InvariantCulture) -lt 1.6) {
            Aviso "o modelo small é lento para ao vivo neste computador; usando o base"
            Baixar "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base-q8_0.bin" (Join-Path $Modelos 'ggml-base-q8_0.bin')
            (Get-Content $config) -replace '^MODELO_AOVIVO=.*', 'MODELO_AOVIVO=base' | Set-Content -Encoding UTF8 $config
        }
    }
    if (-not $env:TCE_TESTE) {
        $ErrorActionPreference = 'Continue'
        & $cmd diagnostico
        $ErrorActionPreference = 'Stop'
    }
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "Tudo pronto!" -ForegroundColor Green
    Write-Host @"

Como usar:
  * Abra um cmd NOVO (ou o atalho 'TCE Transcritor' na área de trabalho) e digite:  tce
  * Ou direto:  tce aovivo LINK   |   tce gravada LINK   |   tce arquivo   |   tce ajuda
  * Os TXT ficam em Downloads\TCE-Transcricoes.
"@
} catch {
    Write-Host ""
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Rode o mesmo comando de novo; ele continua de onde parou." -ForegroundColor Red
    exit 1
}
