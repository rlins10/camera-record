#!/usr/bin/env python3
"""
camera-daemon - Gravação contínua de câmeras IP (RTSP) usando FFmpeg.

Funcionamento:
    - Lê ``~/.config/camera-record/config.ini``. Cada seção diferente de [global] é uma
      câmera (o nome da seção vira o nome da pasta).
    - Para cada câmera sobe uma thread que mantém um processo FFmpeg gravando em
      segmentos (padrão: 60 s), sem recodificar o vídeo (``-c:v copy``).
    - Estrutura gravada: ``<base_dir>/<camera>/<AAAA-MM-DD>/<camera>_AAAAMMDD_HHMMSS.mp4``.
    - À meia-noite o FFmpeg encerra sozinho (``-t``) e o ciclo recomeça numa pasta nova.
    - Se a câmera cair, o FFmpeg é relançado a cada 5 s. Após 3 alertas seguidos as
      mensagens são suprimidas até a conexão voltar, para não inundar o journal.

Robustez:
    - Os segmentos MP4 são fragmentados (``frag_keyframe+empty_moov``): se faltar energia
      ou o processo for morto, o arquivo em escrita continua reproduzível.
    - Timeout de socket RTSP (quando suportado pelo FFmpeg), para não travar numa rede
      que caiu sem fechar a conexão.
    - Exceções inesperadas numa câmera não derrubam a thread: ela registra e tenta de novo.
    - SIGTERM/SIGINT encerram os FFmpeg de forma limpa (os arquivos são finalizados).
    - Usuário e senha da URL são mascarados em qualquer mensagem de log.
    - Se ``mount_point`` estiver configurado, o daemon só grava com o disco montado.

Códigos de saída:
    0 = encerramento normal / serviço não configurado (configured = 0)
    2 = erro de configuração ou FFmpeg ausente (o systemd não tenta reiniciar)

Uso normal: instalado como serviço de usuário pelo comando ``camera-install``.
Uso manual (teste): ``camera-daemon``
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import camera_common as cc

LIMITE_FLOOD = 3          # alertas seguidos exibidos antes de suprimir mensagens
ESPERA_REINICIO = 5       # segundos entre uma tentativa e outra do FFmpeg
ESTABILIDADE_MIN = 30     # segundos gravando que contam como "conexão boa"
TIMEOUT_RTSP_US = "10000000"  # 10 s, em microssegundos

_cache_timeout = None     # opção de timeout RTSP detectada (cache)


_trava_log = threading.Lock()


def log(mensagem):
    """
    Imprime uma linha de log imediatamente (sem buffer) para o journal do systemd.

    Usa um lock e uma única escrita: sem isso, linhas de câmeras diferentes (threads)
    podem se misturar, pois ``print`` escreve o texto e o ``\\n`` em chamadas separadas.
    """
    with _trava_log:
        sys.stdout.write(mensagem + "\n")
        sys.stdout.flush()


def mascarar_credenciais(texto):
    """Troca ``rtsp://usuario:senha@`` por ``rtsp://***@`` para não vazar senha em logs."""
    return re.sub(r"(rtsps?://)[^@\s/]+@", r"\1***@", texto)


def opcao_timeout_rtsp():
    """
    Descobre a opção de timeout de socket do demuxer RTSP da versão instalada do FFmpeg.

    Nas versões >= 5.0 a opção é ``-timeout`` (microssegundos); nas anteriores é
    ``-stimeout``. Como em versões antigas ``-timeout`` significa outra coisa, só é usada
    quando a ajuda do FFmpeg confirma o significado. Retorna uma lista de argumentos
    (vazia se não for possível detectar).
    """
    global _cache_timeout
    if _cache_timeout is not None:
        return _cache_timeout
    opcao = []
    try:
        saida = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", "demuxer=rtsp"],
            capture_output=True, text=True, errors="replace", timeout=20,
        )
        ajuda = (saida.stdout or "") + (saida.stderr or "")
        if re.search(r"^\s*-stimeout\b", ajuda, re.M):
            opcao = ["-stimeout", TIMEOUT_RTSP_US]
        else:
            linha = re.search(r"^\s*-timeout\b.*$", ajuda, re.M)
            if linha and "microsecond" in linha.group(0).lower():
                opcao = ["-timeout", TIMEOUT_RTSP_US]
    except (OSError, subprocess.SubprocessError):
        pass
    _cache_timeout = opcao
    return opcao


def args_entrada(url_camera, dura_segundos):
    """Argumentos de entrada do FFmpeg: RTSP sobre TCP, timestamps pelo relógio e duração."""
    return (
        ["-rtsp_transport", "tcp"]
        + opcao_timeout_rtsp()
        + ["-use_wallclock_as_timestamps", "1", "-t", str(dura_segundos), "-i", url_camera]
    )


def args_saida(nome_camera, dir_hoje, cfg):
    """
    Argumentos de saída do FFmpeg: seleciona só o vídeo (e o áudio, se ``audio = aac``) e
    grava segmentos MP4 fragmentados nomeados com data e hora.
    """
    saida = ["-map", "0:v:0", "-c:v", "copy"]
    if cfg["audio"] == "aac":
        saida += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k"]
    else:
        saida += ["-an"]
    padrao = os.path.join(dir_hoje, "%s_%%Y%%m%%d_%%H%%M%%S.mp4" % nome_camera)
    saida += [
        "-f", "segment",
        "-segment_time", str(cfg["segment_time"]),
        "-segment_format", "mp4",
        # flush_packets=1 grava cada pacote no disco na hora: sem isso, parte do arquivo fica
        # no buffer e se perde (arquivo ilegível) se o processo for morto ou faltar energia.
        "-segment_format_options",
        "movflags=+frag_keyframe+empty_moov+default_base_moof:flush_packets=1",
        "-reset_timestamps", "1",
        "-strftime", "1",
        "-metadata", "title=%s" % nome_camera,
        padrao,
    ]
    return saida


def montar_comando(nome_camera, url_camera, dir_hoje, dura_segundos, cfg):
    """Monta a linha de comando completa do FFmpeg para um ciclo de gravação."""
    base = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats"]
    return (
        base
        + args_entrada(url_camera, dura_segundos)
        + args_saida(nome_camera, dir_hoje, cfg)
    )


def montagem_ok(cfg):
    """True se não há ``mount_point`` configurado ou se ele está efetivamente montado."""
    ponto = cfg["mount_point"]
    return (not ponto) or os.path.ismount(ponto)


def encerrar_ffmpeg(processo):
    """Pede ao FFmpeg para terminar (ele finaliza o arquivo) e força após 15 s."""
    processo.terminate()
    try:
        processo.wait(timeout=15)
    except subprocess.TimeoutExpired:
        processo.kill()
        processo.wait()


def _drenar_stderr(fluxo, ultimas):
    """Lê o stderr do FFmpeg guardando só as últimas linhas (memória sempre limitada)."""
    for linha in iter(fluxo.readline, ""):
        linha = linha.strip()
        if linha:
            ultimas.append(linha)


def alertar_falha(nome_camera, falhas, detalhe):
    """Registra a falha respeitando o limite anti-flood de mensagens."""
    if falhas <= LIMITE_FLOOD:
        log("[%s] ALERTA %d/%d: %s" % (nome_camera, falhas, LIMITE_FLOOD, detalhe))
    elif falhas == LIMITE_FLOOD + 1:
        log("[%s] Limite de alertas atingido. Suprimindo mensagens até reconectar..." % nome_camera)


def executar_ciclo(nome_camera, url_camera, cfg, parar, falhas):
    """
    Executa um ciclo de gravação (um processo FFmpeg) e devolve o novo contador de falhas.

    O contador só volta a zero quando o FFmpeg realmente gravou por um tempo mínimo
    (``ESTABILIDADE_MIN``), de modo que um stream que conecta e cai logo em seguida
    continua contando como falha.
    """
    if not montagem_ok(cfg):
        falhas += 1
        alertar_falha(nome_camera, falhas, "ponto de montagem %s não está montado; aguardando."
                      % cfg["mount_point"])
        return falhas

    agora = datetime.now()
    meia_noite = (agora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    dura_segundos = int((meia_noite - agora).total_seconds())
    if dura_segundos < 10:
        # Faltam poucos segundos para o dia virar: espera, em vez de criar um arquivo inútil.
        parar.wait(dura_segundos + 1)
        return falhas

    dir_hoje = os.path.join(cfg["base_dir"], nome_camera, agora.strftime(cfg["dir_format"]))
    os.makedirs(dir_hoje, exist_ok=True)

    comando = montar_comando(nome_camera, url_camera, dir_hoje, dura_segundos, cfg)
    ultimas = deque(maxlen=5)
    inicio = time.monotonic()
    processo = subprocess.Popen(
        comando, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )
    leitor = threading.Thread(target=_drenar_stderr, args=(processo.stderr, ultimas), daemon=True)
    leitor.start()

    while processo.poll() is None:
        if parar.wait(1):
            encerrar_ffmpeg(processo)
            break
    leitor.join(timeout=3)
    processo.stderr.close()

    if parar.is_set():
        return falhas

    codigo = processo.returncode
    rodou = time.monotonic() - inicio

    if rodou >= ESTABILIDADE_MIN:
        if falhas > 0:
            log("[%s] Conexão RESTABELECIDA (gravou por %ds)." % (nome_camera, int(rodou)))
        falhas = 0

    if codigo == 0 and rodou >= dura_segundos - 5:
        log("[%s] Ciclo diário concluído. Iniciando novo dia." % nome_camera)
        return falhas

    falhas += 1
    if codigo == 0:
        detalhe = "Fluxo encerrado antes do previsto (após %ds)." % int(rodou)
    else:
        detalhe = "Falha (código %s, após %ds)." % (codigo, int(rodou))
    alertar_falha(nome_camera, falhas, detalhe)
    if falhas <= LIMITE_FLOOD and ultimas:
        log("[%s] Detalhe: %s" % (nome_camera, mascarar_credenciais(" | ".join(ultimas))))
    return falhas


def gravar_camera(nome_camera, url_camera, cfg, parar):
    """
    Laço de gravação de uma câmera (executado numa thread).

    Repete ciclos de FFmpeg até ``parar`` ser acionado. Qualquer exceção é registrada e o
    laço continua: uma câmera com problema nunca derruba as outras nem a própria thread.
    """
    log("[%s] Iniciando monitoramento da câmera..." % nome_camera)
    falhas = 0
    while not parar.is_set():
        try:
            falhas = executar_ciclo(nome_camera, url_camera, cfg, parar, falhas)
        except Exception as erro:  # noqa: BLE001 - a thread precisa sobreviver
            falhas += 1
            alertar_falha(nome_camera, falhas,
                          "Erro inesperado: %s" % mascarar_credenciais(str(erro)))
        parar.wait(ESPERA_REINICIO)
    log("[%s] Gravação encerrada." % nome_camera)


def listar_cameras(config):
    """
    Devolve a lista ``[(nome, url), ...]`` das câmeras válidas do arquivo de configuração.

    Seções com nome inválido, sem ``url`` ou com URL que não seja rtsp:// / rtsps://
    são ignoradas com um aviso.
    """
    cameras = []
    for secao in config.sections():
        if secao == "global":
            continue
        if not cc.RE_NOME_CAMERA.match(secao):
            log("AVISO: câmera [%s] ignorada: use só letras, números, _ e - no nome." % secao)
            continue
        url = (config.get(secao, "url", fallback="") or "").strip()
        if not url.lower().startswith(("rtsp://", "rtsps://")):
            log("AVISO: câmera [%s] ignorada: 'url' ausente ou não começa com rtsp://" % secao)
            continue
        cameras.append((secao, url))
    return cameras


def main():
    """Ponto de entrada do ``camera-daemon``. Retorna o código de saída do processo."""
    arquivo = cc.arquivo_config()
    if not arquivo.exists():
        cc.criar_config_padrao()
        log("Arquivo de configuração criado em: %s" % arquivo)
        log("Edite-o, configure as câmeras, mude 'configured = 1' e rode novamente.")
        return 0

    try:
        config = cc.carregar_config(arquivo)
        if not cc.esta_configurado(config):
            log("ATENÇÃO: serviço desativado (configured = 0 em %s)." % arquivo)
            return 0
        cfg = cc.ler_global(config)
    except cc.ConfigError as erro:
        log("ERRO de configuração: %s" % erro)
        return 2

    if shutil.which("ffmpeg") is None:
        log("ERRO: o FFmpeg não foi encontrado. Instale-o (ex.: sudo apt install ffmpeg).")
        return 2

    cameras = listar_cameras(config)
    if not cameras:
        log("ERRO: nenhuma câmera válida configurada em %s." % arquivo)
        return 2

    parar = threading.Event()

    def _sinal(_numero, _quadro):
        parar.set()

    signal.signal(signal.SIGTERM, _sinal)
    signal.signal(signal.SIGINT, _sinal)

    avisou = False
    while not montagem_ok(cfg) and not parar.is_set():
        if not avisou:
            log("Aguardando a montagem de %s antes de gravar..." % cfg["mount_point"])
            avisou = True
        parar.wait(10)

    threads = []
    for nome, url in cameras:
        t = threading.Thread(target=gravar_camera, args=(nome, url, cfg, parar), daemon=True)
        t.start()
        threads.append(t)
    log("%d câmera(s) em gravação. Destino: %s" % (len(threads), cfg["base_dir"]))

    while not parar.is_set():
        parar.wait(1)
    log("Encerrando o serviço de gravação...")
    for t in threads:
        t.join(timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
