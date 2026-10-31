"""Testes do camera-record (somente biblioteca padrão): python -m unittest discover -s tests"""

import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import camera_cleaner  # noqa: E402
import camera_common as cc  # noqa: E402
import camera_installer  # noqa: E402
import camera_record  # noqa: E402


class BaseHome(unittest.TestCase):
    """Isola HOME para que nenhum teste toque nos arquivos reais do usuário."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        os.environ.pop("XDG_CONFIG_HOME", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def escrever_config(self, texto):
        arquivo = cc.arquivo_config()
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(texto, encoding="utf-8")
        return arquivo


class TestConfig(BaseHome):
    def test_config_padrao_criada_com_permissao_600(self):
        self.assertTrue(cc.criar_config_padrao())
        self.assertFalse(cc.criar_config_padrao())  # nunca sobrescreve
        modo = stat.S_IMODE(cc.arquivo_config().stat().st_mode)
        self.assertEqual(modo, 0o600)
        cfg = cc.carregar_config()
        self.assertFalse(cc.esta_configurado(cfg))
        self.assertEqual(cc.ler_global(cfg)["descarte"], 3)

    def test_percentual_e_senha_com_arroba_codificada(self):
        self.escrever_config(
            "[global]\nconfigured = 1\ndir_format = %Y-%m-%d\n"
            "[cam1]\nurl = rtsp://u:p%40ss@10.0.0.1/x?a=1&b=2\n"
        )
        cfg = cc.carregar_config()
        self.assertEqual(cfg.get("cam1", "url"), "rtsp://u:p%40ss@10.0.0.1/x?a=1&b=2")
        self.assertEqual(cc.ler_global(cfg)["dir_format"], "%Y-%m-%d")

    def test_valores_invalidos(self):
        casos = [
            "descarte = 0", "descarte = tres", "descarte = -1",
            "dir_format = %d-%m", "dir_format = %Y", "dir_format = %Y/%m/%d",
            "audio = mp3", "segment_time = 1", "base_dir = relativo/pasta",
            "base_dir = /tmp/a%b", "espaco_minimo_pct = 95",
        ]
        for linha in casos:
            with self.subTest(linha):
                self.escrever_config("[global]\nconfigured = 1\n%s\n" % linha)
                with self.assertRaises(cc.ConfigError):
                    cc.ler_global(cc.carregar_config())

    def test_sem_arquivo_e_sem_global(self):
        with self.assertRaises(cc.ConfigError):
            cc.carregar_config()
        self.escrever_config("[cam1]\nurl = rtsp://x\n")
        with self.assertRaises(cc.ConfigError):
            cc.carregar_config()


class TestDaemon(BaseHome):
    def cfg(self, **extra):
        base = {"audio": "off", "segment_time": 60, "mount_point": "",
                "base_dir": "/tmp/x", "dir_format": "%Y-%m-%d"}
        base.update(extra)
        return base

    def test_mascara_credenciais(self):
        texto = "falha em rtsp://admin:segredo@10.0.0.1:554/x e rtsps://a:b@h/y"
        mascarado = camera_record.mascarar_credenciais(texto)
        self.assertNotIn("segredo", mascarado)
        self.assertNotIn("a:b", mascarado)
        self.assertIn("rtsp://***@10.0.0.1", mascarado)

    def test_comando_ffmpeg(self):
        cmd = camera_record.montar_comando("cam1", "rtsp://h/x", "/d/2026-10-30", 100, self.cfg())
        self.assertLess(cmd.index("-t"), cmd.index("-i"))  # -t é opção de ENTRADA
        self.assertIn("-an", cmd)
        self.assertIn("frag_keyframe", " ".join(cmd))
        self.assertTrue(cmd[-1].endswith("cam1_%Y%m%d_%H%M%S.mp4"))
        com_audio = camera_record.montar_comando(
            "cam1", "rtsp://h/x", "/d", 100, self.cfg(audio="aac"))
        self.assertIn("aac", com_audio)
        self.assertNotIn("-an", com_audio)

    def test_listar_cameras_ignora_invalidas(self):
        self.escrever_config(
            "[global]\nconfigured = 1\n"
            "[ok_1]\nurl = rtsp://h/1\n[nome ruim]\nurl = rtsp://h/2\n"
            "[sem_url]\nfoo = bar\n[http]\nurl = http://h/3\n"
        )
        cfg = cc.carregar_config()
        with mock.patch.object(camera_record, "log"):
            self.assertEqual(camera_record.listar_cameras(cfg), [("ok_1", "rtsp://h/1")])

    def test_main_sem_configuracao_e_nao_configurado(self):
        with mock.patch.object(camera_record, "log"):
            self.assertEqual(camera_record.main(), 0)       # cria o padrão
            self.assertTrue(cc.arquivo_config().exists())
            self.assertEqual(camera_record.main(), 0)       # configured = 0
            self.escrever_config("[global]\nconfigured = 1\ndescarte = 0\n")
            self.assertEqual(camera_record.main(), 2)       # config inválida
            self.escrever_config("[global]\nconfigured = 1\n")
            self.assertEqual(camera_record.main(), 2)       # sem câmeras

    def test_ciclo_falha_conta_e_nao_zera_com_conexao_instantanea(self):
        """Stub de ffmpeg que sai com erro logo: o contador de falhas deve subir."""
        stub = Path(self.tmp.name) / "bin"
        stub.mkdir()
        ff = stub / "ffmpeg"
        ff.write_text("#!/bin/sh\necho 'erro em rtsp://u:senha@h/x' >&2\nexit 1\n")
        ff.chmod(0o755)
        camera_record._cache_timeout = []
        cfg = self.cfg(base_dir=str(Path(self.tmp.name) / "rec"))
        saida = []
        with mock.patch.dict(os.environ, {"PATH": "%s:%s" % (stub, os.environ["PATH"])}), \
                mock.patch.object(camera_record, "log", side_effect=saida.append):
            falhas = 0
            parar = threading.Event()
            for _ in range(5):
                falhas = camera_record.executar_ciclo("c1", "rtsp://u:senha@h/x", cfg, parar, falhas)
        self.assertEqual(falhas, 5)
        texto = "\n".join(saida)
        self.assertIn("ALERTA 1/3", texto)
        self.assertIn("ALERTA 3/3", texto)
        self.assertIn("Suprimindo", texto)
        self.assertNotIn("ALERTA 4", texto)
        self.assertNotIn("senha", texto)

    def test_gravacao_real_com_ffmpeg_e_arquivo_legivel(self):
        """Usa o ffmpeg real com uma fonte de teste no lugar do RTSP e valida os segmentos."""
        if not (subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0
                and subprocess.run(["which", "ffprobe"], capture_output=True).returncode == 0):
            self.skipTest("ffmpeg/ffprobe indisponíveis")
        destino = Path(self.tmp.name) / "cam1" / "2026-10-30"
        destino.mkdir(parents=True)
        cfg = self.cfg(segment_time=10)
        # Fonte de teste já codificada (como o stream de uma câmera), com quadro-chave a cada 1 s.
        fonte = Path(self.tmp.name) / "fonte.ts"
        codecs = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                capture_output=True, text=True).stdout
        codec = "libx264" if "libx264" in codecs else "mpeg4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=size=320x240:rate=10:duration=24", "-c:v", codec, "-g", "10",
             str(fonte)], check=True, timeout=120)
        cmd = (["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-re", "-i", str(fonte)]
               + camera_record.args_saida("cam1", str(destino), cfg))
        subprocess.run(cmd, check=True, timeout=120)
        arquivos = sorted(destino.glob("cam1_*.mp4"))
        self.assertGreaterEqual(len(arquivos), 2)
        for arq in arquivos:
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                                "-of", "csv=p=0", str(arq)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(r.stdout.strip(), ("h264", "mpeg4"))


    def test_segmento_interrompido_com_kill9_continua_legivel(self):
        """Regressão: sem flush_packets=1 o arquivo em escrita ficava com 28 bytes e ilegível."""
        if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
            self.skipTest("ffmpeg indisponível")
        self.assertIn("flush_packets=1", " ".join(
            camera_record.args_saida("c", "/d", self.cfg())))
        fonte = Path(self.tmp.name) / "fonte_k9.ts"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=size=320x240:rate=10:duration=20", "-c:v", "mpeg4", "-g", "10",
             str(fonte)], check=True, timeout=120)
        destino = Path(self.tmp.name) / "k9"
        destino.mkdir()
        cmd = (["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-re", "-i", str(fonte)]
               + camera_record.args_saida("k9", str(destino), self.cfg()))
        processo = subprocess.Popen(cmd)
        import time
        time.sleep(4)
        processo.kill()
        processo.wait()
        arquivo = next(destino.glob("k9_*.mp4"))
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(arquivo)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "arquivo ilegível após kill -9: %s" % r.stderr)
        self.assertGreater(float(r.stdout.strip()), 1.0)


class TestCleaner(BaseHome):
    def montar(self, dias, extra=""):
        base = Path(self.tmp.name) / "rec"
        hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for cam in ("cam1", "cam2"):
            for d in dias:
                pasta = base / cam / (hoje - timedelta(days=d)).strftime("%Y-%m-%d")
                pasta.mkdir(parents=True)
                (pasta / "video.mp4").write_bytes(b"x" * 10)
        (base / "cam1" / "backup_manual").mkdir()
        (base / "arquivo_solto.txt").write_text("nao apagar")
        self.escrever_config(
            "[global]\nconfigured = 1\nbase_dir = %s\ndescarte = 3\n%s" % (base, extra))
        return base, hoje

    def test_apaga_so_o_que_passou_do_limite(self):
        base, hoje = self.montar([0, 1, 2, 3, 4, 5, 10])
        with mock.patch.object(camera_cleaner, "log"):
            self.assertEqual(camera_cleaner.main(), 0)
        for cam in ("cam1", "cam2"):
            restantes = sorted(p.name for p in (base / cam).iterdir())
            esperadas = sorted((hoje - timedelta(days=d)).strftime("%Y-%m-%d") for d in (0, 1, 2, 3))
            if cam == "cam1":
                esperadas = sorted(esperadas + ["backup_manual"])
            self.assertEqual(restantes, esperadas)
        self.assertTrue((base / "arquivo_solto.txt").exists())

    def test_configuracao_perigosa_e_recusada(self):
        base, _ = self.montar([0, 1])
        for extra in ("descarte = 0\n", "dir_format = %d-%m\n"):
            self.escrever_config("[global]\nconfigured = 1\nbase_dir = %s\n%s" % (base, extra))
            with mock.patch.object(camera_cleaner, "log"):
                self.assertEqual(camera_cleaner.main(), 2)
            self.assertEqual(len(list((base / "cam1").iterdir())), 3)  # nada apagado

    def test_nao_configurado_e_sem_arquivo(self):
        with mock.patch.object(camera_cleaner, "log"):
            self.assertEqual(camera_cleaner.main(), 2)  # sem arquivo
            self.escrever_config("[global]\nconfigured = 0\n")
            self.assertEqual(camera_cleaner.main(), 0)

    def test_limpeza_por_espaco_nunca_apaga_hoje(self):
        base, hoje = self.montar([0, 1, 2])
        # força "disco cheio": 100% mínimo é impossível, então apaga tudo que não é de hoje
        with mock.patch.object(camera_cleaner, "log"), \
                mock.patch.object(camera_cleaner, "percentual_livre", return_value=1.0):
            self.escrever_config(
                "[global]\nconfigured = 1\nbase_dir = %s\ndescarte = 3\nespaco_minimo_pct = 20\n" % base)
            self.assertEqual(camera_cleaner.main(), 0)
        hoje_nome = hoje.strftime("%Y-%m-%d")
        for cam in ("cam1", "cam2"):
            datas = [p.name for p in (base / cam).iterdir() if p.name != "backup_manual"]
            self.assertEqual(datas, [hoje_nome])


class TestInstaller(BaseHome):
    def stubs(self, linger="no"):
        pasta = Path(self.tmp.name) / "stubs"
        pasta.mkdir(exist_ok=True)
        registro = Path(self.tmp.name) / "chamadas.log"
        (pasta / "systemctl").write_text('#!/bin/sh\necho "systemctl $@" >> "%s"\nexit 0\n' % registro)
        (pasta / "loginctl").write_text(
            '#!/bin/sh\necho "loginctl $@" >> "%s"\n'
            'case "$1" in show-user) echo "Linger=%s";; esac\nexit 0\n' % (registro, linger))
        (pasta / "ffmpeg").write_text("#!/bin/sh\nexit 0\n")
        for arq in pasta.iterdir():
            arq.chmod(0o755)
        return pasta, registro

    def rodar(self, argv):
        pasta, registro = self.stubs()
        with mock.patch.dict(os.environ, {"PATH": "%s:%s" % (pasta, os.environ["PATH"])}), \
                mock.patch("builtins.print"):
            codigo = camera_installer.main(argv)
        chamadas = registro.read_text().splitlines() if registro.exists() else []
        return codigo, chamadas

    def test_instala_sem_configurar(self):
        codigo, chamadas = self.rodar([])
        self.assertEqual(codigo, 0)
        unidades = self.home / ".config" / "systemd" / "user"
        for nome in ("camera-daemon.service", "camera-cleaner.service", "camera-cleaner.timer"):
            self.assertTrue((unidades / nome).is_file(), nome)
        daemon = (unidades / "camera-daemon.service").read_text()
        self.assertIn(' -m camera_record', daemon)
        self.assertIn("WantedBy=default.target", daemon)
        self.assertNotIn("User=", daemon)
        self.assertIn("OnCalendar=*-*-* 03:00:00", (unidades / "camera-cleaner.timer").read_text())
        self.assertTrue(cc.arquivo_config().exists())
        self.assertIn("systemctl --user daemon-reload", chamadas)
        self.assertIn("loginctl enable-linger", chamadas)
        self.assertIn("systemctl --user enable --now camera-cleaner.timer", chamadas)
        self.assertIn("systemctl --user enable camera-daemon.service", chamadas)
        self.assertNotIn("systemctl --user restart camera-daemon.service", chamadas)  # configured=0

    def test_instala_configurado_inicia_daemon_e_preserva_config(self):
        self.escrever_config("[global]\nconfigured = 1\n[c1]\nurl = rtsp://h/1\n")
        os.chmod(str(cc.arquivo_config()), 0o644)
        antes = cc.arquivo_config().read_text()
        codigo, chamadas = self.rodar(["--hora-limpeza", "04:30"])
        self.assertEqual(codigo, 0)
        self.assertEqual(cc.arquivo_config().read_text(), antes)
        self.assertEqual(stat.S_IMODE(cc.arquivo_config().stat().st_mode), 0o600)
        self.assertIn("systemctl --user restart camera-daemon.service", chamadas)
        timer = (self.home / ".config/systemd/user/camera-cleaner.timer").read_text()
        self.assertIn("04:30:00", timer)

    def test_hora_invalida_e_desinstalacao(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            camera_installer.main(["--hora-limpeza", "25:99"])
        self.rodar([])
        codigo, chamadas = self.rodar(["--uninstall", "--purge-config"])
        self.assertEqual(codigo, 0)
        self.assertFalse((self.home / ".config/systemd/user/camera-daemon.service").exists())
        self.assertFalse(cc.diretorio_config().exists())
        self.assertIn("systemctl --user disable --now camera-cleaner.timer", chamadas)


if __name__ == "__main__":
    unittest.main()
