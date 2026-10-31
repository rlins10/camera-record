#!/usr/bin/env python3
"""
camera-cleaner - Descarte automático das gravações antigas (rotação por dias).

Funcionamento:
    - Percorre ``<base_dir>/<camera>/<pasta-do-dia>`` e apaga as pastas diárias mais
      antigas que a política de descarte. Com ``descarte = 3`` ficam hoje + 3 dias
      anteriores; as pastas anteriores a (hoje - 3 dias) são removidas.
    - Só apaga pastas cujo nome seja uma data válida no formato ``dir_format``; qualquer
      outro arquivo ou pasta dentro de ``base_dir`` é ignorado. Links simbólicos também.
    - Opcional (``espaco_minimo_pct`` > 0): se mesmo assim o espaço livre do disco ficar
      abaixo do percentual configurado, apaga os dias mais antigos, um a um, até
      recuperar o espaço. O dia de hoje nunca é apagado.

Segurança:
    - ``descarte`` precisa ser >= 1 e ``dir_format`` precisa conter ano, mês e dia; valores
      que fariam o script apagar a gravação em andamento são recusados com erro.
    - Se ``mount_point`` estiver configurado e desmontado, nada é apagado.

Códigos de saída:
    0 = limpeza concluída (ou serviço não configurado: configured = 0)
    1 = alguma pasta não pôde ser removida, ou ponto de montagem ausente
    2 = erro de configuração

Uso normal: executado todos os dias pelo timer de usuário ``camera-cleaner.timer``
(criado pelo comando ``camera-install``). Uso manual: ``camera-cleaner``
"""

import os
import shutil
import sys
from datetime import datetime, timedelta

import camera_common as cc


def log(mensagem):
    """Imprime uma linha de log imediatamente para o journal do systemd."""
    print(mensagem, flush=True)


def listar_pastas(base_dir, dir_format):
    """
    Lista as pastas diárias existentes como tuplas ``(data, caminho)``.

    Ignora arquivos, links simbólicos e pastas cujo nome não seja uma data no formato
    ``dir_format``.
    """
    pastas = []
    for nome_camera in sorted(os.listdir(base_dir)):
        cam_path = os.path.join(base_dir, nome_camera)
        if os.path.islink(cam_path) or not os.path.isdir(cam_path):
            continue
        for pasta_data in sorted(os.listdir(cam_path)):
            pasta_path = os.path.join(cam_path, pasta_data)
            if os.path.islink(pasta_path) or not os.path.isdir(pasta_path):
                continue
            try:
                data_pasta = datetime.strptime(pasta_data, dir_format)
            except ValueError:
                continue
            pastas.append((data_pasta, pasta_path))
    return pastas


def apagar_pasta(caminho):
    """Remove uma pasta diária. Retorna True em caso de sucesso, False se falhar."""
    log("[Descarte] Apagando: %s" % caminho)
    try:
        shutil.rmtree(caminho)
        return True
    except OSError as erro:
        log("Erro ao remover %s: %s" % (caminho, erro))
        return False


def percentual_livre(caminho):
    """Percentual de espaço livre do sistema de arquivos que contém ``caminho``."""
    uso = shutil.disk_usage(caminho)
    return 100.0 * uso.free / uso.total


def limpar_por_data(pastas, data_limite):
    """Apaga as pastas anteriores a ``data_limite``. Retorna ``(apagadas, erros)``."""
    apagadas = erros = 0
    for data_pasta, caminho in pastas:
        if data_pasta < data_limite:
            if apagar_pasta(caminho):
                apagadas += 1
            else:
                erros += 1
    return apagadas, erros


def limpar_por_espaco(base_dir, pastas, hoje, minimo_pct):
    """
    Apaga os dias mais antigos (de qualquer câmera) até o espaço livre atingir ``minimo_pct``.

    Nunca toca em pastas de hoje ou futuras. Retorna ``(apagadas, erros)``.
    """
    apagadas = erros = 0
    candidatas = sorted(p for p in pastas if p[0] < hoje and os.path.isdir(p[1]))
    for _data, caminho in candidatas:
        livre = percentual_livre(base_dir)
        if livre >= minimo_pct:
            break
        log("Espaço livre %.1f%% < %d%%: liberando espaço." % (livre, minimo_pct))
        if apagar_pasta(caminho):
            apagadas += 1
        else:
            erros += 1
    livre = percentual_livre(base_dir)
    if livre < minimo_pct:
        log("AVISO: espaço livre continua em %.1f%% (mínimo %d%%) e não há mais dias antigos "
            "para apagar." % (livre, minimo_pct))
    return apagadas, erros


def main():
    """Ponto de entrada do ``camera-cleaner``. Retorna o código de saída do processo."""
    try:
        arquivo = cc.arquivo_config()
        config = cc.carregar_config(arquivo)
        if not cc.esta_configurado(config):
            log("Serviço não configurado (configured = 0). Abortando limpeza.")
            return 0
        cfg = cc.ler_global(config)
    except cc.ConfigError as erro:
        log("ERRO de configuração: %s" % erro)
        return 2

    base_dir = cfg["base_dir"]
    descarte = cfg["descarte"]
    dir_format = cfg["dir_format"]

    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    data_limite = hoje - timedelta(days=descarte)

    log("Limpeza iniciada. Política de descarte: hoje + %d dia(s) anteriores." % descarte)
    log("Apagando gravações anteriores a: %s" % data_limite.strftime(dir_format))

    if cfg["mount_point"] and not os.path.ismount(cfg["mount_point"]):
        log("ERRO: %s não está montado; nada foi apagado." % cfg["mount_point"])
        return 1
    if not os.path.isdir(base_dir):
        log("Diretório base %s vazio ou inexistente. Nada a fazer." % base_dir)
        return 0

    pastas = listar_pastas(base_dir, dir_format)
    apagadas, erros = limpar_por_data(pastas, data_limite)

    if cfg["espaco_minimo_pct"] > 0:
        restantes = [p for p in pastas if os.path.isdir(p[1])]
        extra_apagadas, extra_erros = limpar_por_espaco(
            base_dir, restantes, hoje, cfg["espaco_minimo_pct"]
        )
        apagadas += extra_apagadas
        erros += extra_erros

    log("Limpeza concluída: %d pasta(s) apagada(s), %d erro(s)." % (apagadas, erros))
    return 1 if erros else 0


if __name__ == "__main__":
    sys.exit(main())
