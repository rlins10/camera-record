#!/usr/bin/env python3
"""
camera_common - funções compartilhadas pelo camera-daemon, camera-cleaner e camera-install.

Centraliza a leitura e a validação do arquivo de configuração para que o daemon e o
script de limpeza sempre interpretem o ``config.ini`` da mesma forma.

Arquivo de configuração: ``~/.config/camera-record/config.ini``

Pontos importantes:
- O ``ConfigParser`` é criado com ``interpolation=None``. Assim o caractere ``%`` pode ser
  usado livremente, tanto em ``dir_format = %Y-%m-%d`` quanto em senhas de URLs RTSP
  (por exemplo ``%40`` no lugar de ``@``), sem causar ``InterpolationSyntaxError``.
- Valores inválidos geram ``ConfigError`` com uma mensagem clara, em vez de um traceback.
"""

import configparser
import os
import re
from datetime import datetime
from pathlib import Path

NOME_APP = "camera-record"

# Nome de câmera vira nome de pasta: só caracteres seguros.
RE_NOME_CAMERA = re.compile(r"^[A-Za-z0-9_-]+$")

CONFIG_PADRAO = """\
# Configuração do camera-record
# Lido pelo camera-daemon e pelo camera-cleaner.
# Este arquivo contém senhas das câmeras: mantenha a permissão 600.

[global]
# Trava de segurança. Deixe 0 até configurar as câmeras abaixo; depois mude para 1
# e reinicie o serviço:  systemctl --user restart camera-daemon
configured = 0

# Diretório onde as gravações são salvas (uma pasta por câmera e por dia).
base_dir = ~/gravacoes

# (Opcional) Ponto de montagem que precisa estar montado para gravar.
# Evita encher o disco do sistema caso o HD/RAID não monte no boot.
# Exemplo: mount_point = /mnt/dmraid      (vazio = desativado)
mount_point =

# Quantos dias anteriores a hoje são mantidos (fica hoje + N dias). Mínimo 1.
descarte = 3

# (Opcional) Limpeza por espaço: se o espaço livre do disco ficar abaixo deste
# percentual, o cleaner apaga os dias mais antigos (nunca o dia de hoje).
# 0 = desativado.
espaco_minimo_pct = 0

# Formato do nome da pasta diária (strftime). Precisa conter %Y, %m e %d.
# Não altere depois que já existirem gravações.
dir_format = %Y-%m-%d

# Duração de cada arquivo de vídeo, em segundos.
segment_time = 60

# Áudio: off = sem áudio (padrão) | aac = converte o áudio da câmera para AAC.
audio = off

# Exemplo de câmera (descomente e ajuste). O nome da seção vira o nome da pasta
# (apenas letras, números, _ e -). Se a senha tiver caracteres especiais, use
# a codificação de URL (@ = %40, : = %3A, / = %2F, % = %25).
#[cam01_frente]
#url = rtsp://usuario:senha@10.10.10.1:554/caminho-do-stream
"""


class ConfigError(Exception):
    """Erro de configuração com mensagem pensada para ser mostrada ao usuário."""


def diretorio_config():
    """Retorna o diretório de configuração do usuário (``~/.config/camera-record``)."""
    return Path.home() / ".config" / NOME_APP


def arquivo_config():
    """Retorna o caminho do ``config.ini`` do usuário."""
    return diretorio_config() / "config.ini"


def criar_config_padrao():
    """
    Cria o ``config.ini`` padrão (permissão 600) se ele ainda não existir.

    Retorna True se o arquivo foi criado e False se já existia (nunca sobrescreve).
    """
    arquivo = arquivo_config()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(arquivo), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(CONFIG_PADRAO)
    return True


def carregar_config(caminho=None):
    """
    Lê o arquivo INI e devolve o ``ConfigParser`` (sem interpolação).

    Levanta ConfigError se o arquivo não existir, estiver malformado ou não tiver [global].
    """
    caminho = Path(caminho) if caminho else arquivo_config()
    if not caminho.is_file():
        raise ConfigError(
            "Arquivo %s não encontrado. Rode 'camera-install' (ou 'camera-daemon') "
            "para criar o arquivo padrão." % caminho
        )
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(str(caminho), encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError) as erro:
        raise ConfigError("Erro ao ler %s: %s" % (caminho, erro))
    if not config.has_section("global"):
        raise ConfigError("O arquivo %s precisa ter a seção [global]." % caminho)
    return config


def _inteiro(config, chave, padrao, minimo=None, maximo=None):
    """Lê um inteiro da seção [global] validando o intervalo permitido."""
    bruto = config.get("global", chave, fallback=None)
    if bruto is None or bruto.strip() == "":
        return padrao
    try:
        valor = int(bruto.strip())
    except ValueError:
        raise ConfigError("'%s' deve ser um número inteiro (valor atual: %r)." % (chave, bruto))
    if minimo is not None and valor < minimo:
        raise ConfigError("'%s' deve ser >= %d (valor atual: %d)." % (chave, minimo, valor))
    if maximo is not None and valor > maximo:
        raise ConfigError("'%s' deve ser <= %d (valor atual: %d)." % (chave, maximo, valor))
    return valor


def esta_configurado(config):
    """True se ``configured`` for diferente de 0 (trava de segurança liberada)."""
    return _inteiro(config, "configured", 0, minimo=0) != 0


def validar_dir_format(dir_format):
    """
    Garante que o formato da pasta diária identifica o dia sem ambiguidade.

    Faz um teste de ida e volta (strftime -> strptime) com uma data de referência: se o
    formato não tiver ano, mês e dia, a data lida seria diferente e o cleaner poderia
    interpretar uma pasta de hoje como antiga. Também recusa ``/`` e ``%`` no resultado.
    """
    referencia = datetime(2001, 2, 3)
    try:
        texto = referencia.strftime(dir_format)
        volta = datetime.strptime(texto, dir_format)
    except ValueError:
        volta = None
        texto = ""
    if volta != referencia:
        raise ConfigError(
            "dir_format %r é inválido: precisa conter %%Y, %%m e %%d (ex.: %%Y-%%m-%%d)."
            % dir_format
        )
    if "/" in texto or "%" in texto or os.sep in texto:
        raise ConfigError("dir_format %r gera nomes de pasta inválidos." % dir_format)
    return dir_format


def ler_global(config):
    """
    Lê e valida a seção [global]. Devolve um dicionário com os valores já tratados.

    Chaves: configured, base_dir, mount_point, descarte, espaco_minimo_pct,
    dir_format, segment_time, audio.
    """
    base_dir = os.path.expanduser(
        (config.get("global", "base_dir", fallback="") or "").strip() or "~/gravacoes"
    )
    if not os.path.isabs(base_dir):
        raise ConfigError("'base_dir' precisa ser um caminho absoluto (valor: %r)." % base_dir)
    if "%" in base_dir:
        raise ConfigError("'base_dir' não pode conter o caractere %% (valor: %r)." % base_dir)
    base_dir = os.path.normpath(base_dir)

    mount_point = os.path.expanduser((config.get("global", "mount_point", fallback="") or "").strip())
    if mount_point and not os.path.isabs(mount_point):
        raise ConfigError("'mount_point' precisa ser um caminho absoluto (valor: %r)." % mount_point)

    audio = (config.get("global", "audio", fallback="off") or "off").strip().lower()
    if audio not in ("off", "aac"):
        raise ConfigError("'audio' deve ser 'off' ou 'aac' (valor atual: %r)." % audio)

    dir_format = (config.get("global", "dir_format", fallback="%Y-%m-%d") or "%Y-%m-%d").strip()

    return {
        "configured": _inteiro(config, "configured", 0, minimo=0),
        "base_dir": base_dir,
        "mount_point": os.path.normpath(mount_point) if mount_point else "",
        "descarte": _inteiro(config, "descarte", 3, minimo=1, maximo=3650),
        "espaco_minimo_pct": _inteiro(config, "espaco_minimo_pct", 0, minimo=0, maximo=90),
        "dir_format": validar_dir_format(dir_format),
        "segment_time": _inteiro(config, "segment_time", 60, minimo=10, maximo=3600),
        "audio": audio,
    }
