#!/usr/bin/env python3
"""
camera-install - Instala (ou remove) o daemon e o timer do camera-record no systemd do USUÁRIO.

Tudo acontece dentro do diretório pessoal e com o systemd em modo usuário
(``systemctl --user``). Não é necessário root nem sudo.

O que o comando faz, nesta ordem:
    1. Cria ``~/.config/camera-record/config.ini`` (permissão 600), se ainda não existir.
       Um arquivo existente nunca é sobrescrito.
    2. Cria o diretório ``~/.config/systemd/user`` e grava nele três unidades:
         - camera-daemon.service   (gravação contínua das câmeras)
         - camera-cleaner.service  (descarte de gravações antigas, one-shot)
         - camera-cleaner.timer    (dispara o descarte todo dia, às 03:00 por padrão)
       As unidades apontam para o Python em que o pacote foi instalado (por exemplo, o do
       seu ambiente virtual), então funcionam em qualquer venv, sem caminhos fixos.
    3. Executa ``systemctl --user daemon-reload``.
    4. Tenta ativar o "linger" (``loginctl enable-linger``) para que os serviços iniciem no
       boot sem precisar de login. Se o sistema exigir um administrador para isso, mostra
       o comando a executar uma única vez.
    5. Habilita e inicia o timer. O daemon só é iniciado se ``configured = 1`` no
       ``config.ini``; caso contrário fica habilitado e aguardando a sua configuração.

O comando é idempotente: pode ser executado novamente após atualizar o pacote
(``pip install -U .``) para regravar as unidades e reiniciar o serviço.

Exemplos:
    camera-install
    camera-install --hora-limpeza 04:30
    camera-install --sem-iniciar
    camera-install --uninstall
    camera-install --uninstall --purge-config
"""

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import camera_common as cc

UNIDADE_DAEMON = "camera-daemon.service"
UNIDADE_CLEANER = "camera-cleaner.service"
UNIDADE_TIMER = "camera-cleaner.timer"
RE_HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

MODELO_DAEMON = """\
[Unit]
Description=Camera Record - gravação de câmeras IP (serviço de usuário)
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={exec_daemon}
Restart=on-failure
RestartSec=10
# Código 2 = erro de configuração: não adianta reiniciar até o usuário corrigir.
RestartPreventExitStatus=2
Environment=PYTHONUNBUFFERED=1
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""

MODELO_CLEANER = """\
[Unit]
Description=Camera Record - descarte de gravações antigas

[Service]
Type=oneshot
ExecStart={exec_cleaner}
Nice=10
IOSchedulingClass=idle
StandardOutput=journal
StandardError=journal
"""

MODELO_TIMER = """\
[Unit]
Description=Camera Record - execução diária do descarte de gravações

[Timer]
OnCalendar=*-*-* {hora}:00
Persistent=true
Unit={unidade}

[Install]
WantedBy=timers.target
"""


def diretorio_unidades():
    """Diretório das unidades do systemd de usuário (respeita XDG_CONFIG_HOME)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _exec(python, modulo):
    """Monta uma linha ExecStart com escape de aspas, % e $ conforme a sintaxe do systemd."""
    caminho = python.replace("\\", "\\\\").replace('"', '\\"')
    caminho = caminho.replace("%", "%%").replace("$", "$$")
    return '"%s" -m %s' % (caminho, modulo)


def gerar_unidades(hora):
    """Devolve ``{nome_do_arquivo: conteúdo}`` das três unidades de usuário."""
    python = os.path.abspath(sys.executable)
    return {
        UNIDADE_DAEMON: MODELO_DAEMON.format(exec_daemon=_exec(python, "camera_record")),
        UNIDADE_CLEANER: MODELO_CLEANER.format(exec_cleaner=_exec(python, "camera_cleaner")),
        UNIDADE_TIMER: MODELO_TIMER.format(hora=hora, unidade=UNIDADE_CLEANER),
    }


def executar(comando):
    """Executa um comando e devolve o ``CompletedProcess`` (ou None se não foi possível)."""
    try:
        return subprocess.run(comando, capture_output=True, text=True, errors="replace", timeout=90)
    except (OSError, subprocess.SubprocessError):
        return None


def systemctl(*args):
    """Executa ``systemctl --user <args>``."""
    return executar(["systemctl", "--user"] + list(args))


def sucesso(resultado):
    """True se o comando executou e retornou 0."""
    return resultado is not None and resultado.returncode == 0


def detalhe(resultado):
    """Texto curto com o erro de um comando que falhou."""
    if resultado is None:
        return "comando não pôde ser executado"
    return (resultado.stderr or resultado.stdout or "").strip() or "código %d" % resultado.returncode


def garantir_linger():
    """Garante o linger do usuário (serviços no boot, sem login). Nunca usa sudo."""
    usuario = getpass.getuser()
    estado = executar(["loginctl", "show-user", usuario, "--property=Linger"])
    if sucesso(estado) and "Linger=yes" in (estado.stdout or ""):
        print("[OK] Linger já ativo: os serviços iniciam no boot, sem login.")
        return
    resultado = executar(["loginctl", "enable-linger"])
    if sucesso(resultado):
        print("[OK] Linger ativado: os serviços iniciam no boot, sem login.")
    else:
        print("[!!] Não foi possível ativar o linger automaticamente (%s)." % detalhe(resultado))
        print("     Sem ele, o daemon só roda enquanto você estiver logado. Peça a um administrador")
        print("     para executar UMA vez:  sudo loginctl enable-linger %s" % usuario)


def instalar(hora, iniciar):
    """Instala configuração, unidades e habilita os serviços. Retorna o código de saída."""
    if not sys.platform.startswith("linux") or shutil.which("systemctl") is None:
        print("ERRO: este instalador requer Linux com systemd (systemctl não encontrado).")
        return 1

    if shutil.which("ffmpeg") is None:
        print("[!!] FFmpeg não encontrado no PATH. Instale-o (ex.: sudo apt install ffmpeg).")

    # 1) Configuração do usuário
    arquivo = cc.arquivo_config()
    if cc.criar_config_padrao():
        print("[OK] Arquivo de configuração criado: %s" % arquivo)
    else:
        print("[OK] Arquivo de configuração já existe (mantido): %s" % arquivo)
        if arquivo.stat().st_mode & 0o077:
            os.chmod(str(arquivo), 0o600)
            print("[OK] Permissão do config.ini ajustada para 600 (contém senhas).")

    # 2) Unidades do systemd de usuário
    destino = diretorio_unidades()
    destino.mkdir(parents=True, exist_ok=True)
    print("[OK] Diretório do systemd de usuário: %s" % destino)
    for nome, conteudo in gerar_unidades(hora).items():
        (destino / nome).write_text(conteudo, encoding="utf-8")
        print("[OK] Unidade gravada: %s" % (destino / nome))

    # 3) Recarrega o systemd de usuário
    resultado = systemctl("daemon-reload")
    if not sucesso(resultado):
        print("ERRO: o systemd de usuário não respondeu (%s)." % detalhe(resultado))
        print("      Os arquivos já foram gravados. Faça login numa sessão normal (ou, via SSH,")
        print("      exporte XDG_RUNTIME_DIR=/run/user/$(id -u)) e rode 'camera-install' de novo.")
        return 1
    print("[OK] systemctl --user daemon-reload")

    # 4) Linger
    garantir_linger()

    # 5) Habilita e inicia
    try:
        configurado = cc.esta_configurado(cc.carregar_config(arquivo))
    except cc.ConfigError as erro:
        print("[!!] %s" % erro)
        configurado = False

    codigo = 0
    for etapa, args in (
        ("timer de limpeza", ["enable", "--now", UNIDADE_TIMER] if iniciar else ["enable", UNIDADE_TIMER]),
        ("daemon", ["enable", UNIDADE_DAEMON]),
    ):
        resultado = systemctl(*args)
        if sucesso(resultado):
            print("[OK] %s habilitado." % etapa.capitalize())
        else:
            print("ERRO ao habilitar %s: %s" % (etapa, detalhe(resultado)))
            codigo = 1

    if configurado and iniciar:
        resultado = systemctl("restart", UNIDADE_DAEMON)
        if sucesso(resultado):
            print("[OK] Daemon iniciado.")
        else:
            print("ERRO ao iniciar o daemon: %s" % detalhe(resultado))
            codigo = 1

    print()
    if not configurado:
        print("PRÓXIMOS PASSOS:")
        print("  1. Edite %s" % arquivo)
        print("     (adicione as câmeras e mude 'configured = 1')")
        print("  2. systemctl --user restart %s" % UNIDADE_DAEMON)
    else:
        print("Instalação concluída.")
    print("Acompanhe com:")
    print("  systemctl --user status camera-daemon")
    print("  journalctl --user -u camera-daemon -f")
    print("  systemctl --user list-timers")
    return codigo


def desinstalar(purgar_config):
    """Desabilita e remove as unidades (e, opcionalmente, o config.ini). Retorna o código."""
    destino = diretorio_unidades()
    if shutil.which("systemctl") is not None:
        for unidade in (UNIDADE_TIMER, UNIDADE_DAEMON):
            systemctl("disable", "--now", unidade)
    removidas = 0
    for nome in (UNIDADE_DAEMON, UNIDADE_CLEANER, UNIDADE_TIMER):
        arquivo = destino / nome
        if arquivo.exists():
            arquivo.unlink()
            removidas += 1
    if shutil.which("systemctl") is not None:
        systemctl("daemon-reload")
    print("[OK] Serviços desabilitados; %d unidade(s) removida(s) de %s" % (removidas, destino))

    if purgar_config:
        pasta = cc.diretorio_config()
        if pasta.exists():
            shutil.rmtree(str(pasta))
            print("[OK] Configuração removida: %s" % pasta)
    else:
        print("     Configuração mantida em %s (use --purge-config para apagar)." % cc.diretorio_config())
    print("     As gravações NÃO foram apagadas. Para remover o pacote: pip uninstall camera-record")
    return 0


def main(argv=None):
    """Ponto de entrada do ``camera-install``. Retorna o código de saída do processo."""
    parser = argparse.ArgumentParser(
        prog="camera-install",
        description="Instala o daemon e o timer do camera-record no systemd do usuário (sem root).",
    )
    parser.add_argument("--uninstall", action="store_true",
                        help="desabilita e remove as unidades do systemd de usuário")
    parser.add_argument("--purge-config", action="store_true",
                        help="junto com --uninstall, apaga também ~/.config/camera-record")
    parser.add_argument("--hora-limpeza", default="03:00", metavar="HH:MM",
                        help="horário diário do descarte (padrão: 03:00)")
    parser.add_argument("--sem-iniciar", action="store_true",
                        help="grava e habilita as unidades, mas não inicia nada agora")
    args = parser.parse_args(argv)

    if args.purge_config and not args.uninstall:
        parser.error("--purge-config só pode ser usado com --uninstall")
    if not RE_HORA.match(args.hora_limpeza):
        parser.error("--hora-limpeza deve estar no formato HH:MM (ex.: 03:00)")

    if args.uninstall:
        return desinstalar(args.purge_config)
    return instalar(args.hora_limpeza, not args.sem_iniciar)


if __name__ == "__main__":
    sys.exit(main())
