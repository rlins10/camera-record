# Camera Record

Gravação contínua de câmeras IP (RTSP) com rotação automática por dias, pensada para pequenos
estabelecimentos. Usa o FFmpeg para gravar **sem recodificar** (baixo uso de CPU) e roda
inteiramente como **serviço de usuário do systemd**: depois de instalado o FFmpeg, nada neste projeto
precisa de root.

Diferenciais : 100% user space, baixo consumo de CPU e RAM, usa os recursos do systemd

- **`camera-daemon`**: uma thread por câmera, cada uma com um FFmpeg gravando arquivos de 60 s em uma pasta por dia.
- **`camera-cleaner`**: apaga as pastas de dias antigos (e, opcionalmente, libera espaço quando o disco enche).
- **`camera-install`**: cria a configuração, os diretórios do systemd de usuário e os serviços/timer, e já os habilita.

```
~/gravacoes/                      (ou o base_dir que você configurar)
└── cam01_frente/
    └── 2026-10-30/
        ├── cam01_frente_20261030_000000.mp4
        ├── cam01_frente_20261030_000100.mp4
        └── ...
```

## Tudo roda como usuário, sem root

| Componente | Onde fica | Quem gerencia |
|---|---|---|
| Daemon de gravação | `~/.config/systemd/user/camera-daemon.service` | `systemctl --user` |
| Timer de limpeza | `~/.config/systemd/user/camera-cleaner.timer` (+ `.service`) | `systemctl --user` |
| Configuração | `~/.config/camera-record/config.ini` (permissão 600) | você |
| Programa | seu ambiente virtual Python | `pip` |

Os comandos `systemctl --user` e `journalctl --user` **não usam `sudo`**. Os arquivos de serviço **não são
copiados à mão**: o `camera-install` cria os diretórios e as unidades por você.

> A única exceção possível: para os serviços iniciarem no boot **sem ninguém logar**, o usuário precisa
> do *linger* (`loginctl enable-linger`). O `camera-install` tenta ativar sozinho, como o próprio usuário.
> Se o seu sistema negar, ele mostra o comando para um administrador rodar **uma única vez**.

## Requisitos

- Linux com systemd (Debian, Ubuntu, Fedora, Arch etc.)
- Python 3.8 ou superior (com `venv` e `pip`)
- FFmpeg: `sudo apt install ffmpeg` (único passo de sistema; não é do projeto)
- Câmeras IP com RTSP habilitado
- Relógio sincronizado (`timedatectl set-ntp true`) e fuso horário correto: nomes de pastas e arquivos usam a hora local

## Instalação

```bash
git clone https://github.com/rlins10/camera-record.git
cd camera-record

python3 -m venv ~/myenv
source ~/myenv/bin/activate
pip install .

camera-install
```

O `camera-install` executa, nesta ordem:

1. Cria `~/.config/camera-record/config.ini` com valores padrão (permissão 600). Se já existir, **não sobrescreve**.
2. Cria `~/.config/systemd/user/` e grava `camera-daemon.service`, `camera-cleaner.service` e `camera-cleaner.timer`, apontando para o Python do seu ambiente virtual.
3. Roda `systemctl --user daemon-reload`.
4. Tenta ativar o linger (serviços no boot, sem login).
5. Habilita e inicia o timer de limpeza. O daemon só inicia se `configured = 1`.

Opções: `--hora-limpeza HH:MM` (padrão `03:00`), `--sem-iniciar`, `--uninstall`, `--purge-config`.

O comando é **idempotente**: após atualizar o pacote (`pip install -U .`), rode `camera-install` de novo para regravar as unidades e reiniciar o serviço.

## Configuração

Edite `~/.config/camera-record/config.ini`:

```ini
[global]
configured = 1
base_dir = /mnt/dmraid/gravacoes
mount_point = /mnt/dmraid
descarte = 3
espaco_minimo_pct = 10
dir_format = %Y-%m-%d
segment_time = 60
audio = off

[cam01_frente]
url = rtsp://usuario:senha@10.10.10.1:554/cam/realmonitor?channel=1&subtype=0

[cam02_quintal]
url = rtsp://usuario:senha@10.10.10.2:554/cam/realmonitor?channel=1&subtype=0
```

Depois de editar, aplique: `systemctl --user restart camera-daemon`. Um exemplo completo está em [`config.ini.example`](config.ini.example).

### Chaves de `[global]`

| Chave | Padrão | Descrição |
|---|---|---|
| `configured` | `0` | Trava de segurança. Com `0`, daemon e cleaner não fazem nada. Mude para `1` após configurar as câmeras. |
| `base_dir` | `~/gravacoes` | Pasta raiz das gravações. Caminho absoluto, sem `%`. |
| `mount_point` | *(vazio)* | Se preenchido, o daemon só grava (e o cleaner só apaga) com esse ponto de montagem ativo. Evita encher o disco do sistema se o HD/RAID não montar. |
| `descarte` | `3` | Dias anteriores a hoje que são mantidos: ficam **hoje + N dias**. Mínimo 1. |
| `espaco_minimo_pct` | `0` | Se o espaço livre ficar abaixo deste percentual (1 a 90), o cleaner apaga os dias mais antigos até recuperar. Nunca apaga o dia de hoje. `0` desativa. |
| `dir_format` | `%Y-%m-%d` | Formato (strftime) da pasta diária. Precisa conter `%Y`, `%m` e `%d`. Não mude com gravações existentes. |
| `segment_time` | `60` | Duração de cada arquivo, em segundos (10 a 3600). |
| `audio` | `off` | `off` grava sem áudio. `aac` converte o áudio da câmera para AAC. Veja a observação legal abaixo. |

### Câmeras

- Cada seção diferente de `[global]` é uma câmera. **O nome da seção vira o nome da pasta**: use só letras, números, `_` e `-`.
- `url` precisa começar com `rtsp://` ou `rtsps://`. Seções inválidas são ignoradas com um aviso no log.
- Senha com caracteres especiais: use codificação de URL (`@` → `%40`, `:` → `%3A`, `/` → `%2F`, `%` → `%25`). O `%` pode ser usado livremente no arquivo.
- Caminhos RTSP comuns (confira no manual da sua câmera):
  - Intelbras/Dahua: `rtsp://user:senha@IP:554/cam/realmonitor?channel=1&subtype=0` (`subtype=1` é o substream)
  - Hikvision: `rtsp://user:senha@IP:554/Streaming/Channels/101` (`102` é o substream)
- Teste a URL antes: `ffplay -rtsp_transport tcp "rtsp://..."`
- O substream ocupa bem menos disco, com resolução menor.

## Operação

```bash
systemctl --user status camera-daemon        # estado do daemon
journalctl --user -u camera-daemon -f        # log em tempo real
systemctl --user restart camera-daemon       # aplicar mudanças no config.ini
systemctl --user list-timers                 # próxima limpeza
systemctl --user start camera-cleaner.service   # forçar uma limpeza agora
journalctl --user -u camera-cleaner --since today
```

### Comportamento do daemon

- À meia-noite o FFmpeg encerra sozinho e uma pasta nova é criada.
- Se a câmera cair, o FFmpeg é relançado a cada 5 s. Após 3 alertas seguidos as mensagens são suprimidas até a conexão voltar ("Conexão RESTABELECIDA").
- Os arquivos MP4 são fragmentados e gravados em tempo real: se faltar energia ou o processo for morto, o arquivo em escrita continua reproduzível.
- Usuário e senha são mascarados (`rtsp://***@`) em qualquer mensagem de log.
- Erro de configuração encerra com código 2 e o systemd **não** fica reiniciando em loop; corrija o arquivo e reinicie o serviço.

### Juntar trechos para exportar

```bash
cd ~/gravacoes/cam01_frente/2026-10-30
ls cam01_frente_20261030_14[0-2]*.mp4 | sed "s/^/file '/;s/$/'/" > lista.txt
ffmpeg -f concat -safe 0 -i lista.txt -c copy trecho.mp4
```

## Dimensionamento de disco

Consumo por câmera ≈ bitrate (Mbps) × 0,45 GB por hora. Com 4 Mbps: ≈ 43 GB por dia por câmera.

`espaço ≈ câmeras × GB/dia × (descarte + 1)`

Exemplo: 3 câmeras de 4 Mbps com `descarte = 3` ≈ 3 × 43 × 4 ≈ **520 GB**, mais margem de 15 a 20%.
Use `espaco_minimo_pct` como segunda proteção caso o bitrate real seja maior que o estimado.

## Atualização e remoção

```bash
# atualizar (dentro do clone, com o venv ativo)
git pull && pip install -U . && camera-install

# remover os serviços (mantém config e gravações)
camera-install --uninstall

# remover também a configuração
camera-install --uninstall --purge-config

# remover o programa
pip uninstall camera-record
```

As gravações **nunca** são apagadas pela desinstalação.

## Solução de problemas

| Sintoma | Causa provável e solução |
|---|---|
| `camera-install`: "o systemd de usuário não respondeu" | Sessão sem barramento de usuário (ex.: `su` ou SSH sem sessão). Faça login normal ou rode `export XDG_RUNTIME_DIR=/run/user/$(id -u)` e execute `camera-install` de novo. Os arquivos já foram gravados. |
| Daemon não sobe após reiniciar o servidor | Falta o linger. Peça a um administrador: `sudo loginctl enable-linger rlins10`. |
| "serviço desativado (configured = 0)" | Falta `configured = 1` no `[global]`; depois `systemctl --user restart camera-daemon`. |
| "ERRO de configuração: ..." | A mensagem diz a chave e o valor inválidos. Corrija e reinicie o serviço. |
| "Aguardando a montagem de ..." | O `mount_point` não está montado. Monte o disco; o daemon continua sozinho. |
| `ALERTA ... Falha (código 1 ...)` | Câmera offline, senha/URL errada ou RTSP desabilitado. Teste com `ffplay`. |
| `ALERTA ... Fluxo encerrado antes do previsto` | A câmera fechou a conexão. Verifique limite de conexões RTSP simultâneas da câmera. |
| Erro de codec com áudio | Mantenha `audio = off` (padrão) ou use `audio = aac`. |
| Nada é apagado | Confira `descarte`, `dir_format` e `journalctl --user -u camera-cleaner`. Só pastas com nome de data válido são apagadas. |

## Segurança e boas práticas

- O `config.ini` contém senhas e é criado com permissão 600. Mantenha assim e **não o publique** (já está no `.gitignore`).
- A URL com senha aparece na lista de processos do FFmpeg (`ps`) para outros usuários da máquina: use um servidor dedicado e uma conta de câmera somente de leitura de vídeo.
- Isole as câmeras em uma rede/VLAN própria, sem acesso à internet, e troque as senhas de fábrica.
- RAID não é backup. Copie para outro local qualquer gravação importante antes que o descarte a apague.
- Em estabelecimentos, sinalize o monitoramento e observe a LGPD: acesso restrito às imagens e prazo de retenção definido. **Gravar áudio de pessoas pode ter exigências legais adicionais**; por isso o padrão é `audio = off`.

## Desenvolvimento

```bash
python3 -m unittest discover -s tests -v
```

Os testes usam apenas a biblioteca padrão (os de gravação exigem `ffmpeg` e `ffprobe` e são ignorados se ausentes) e nunca tocam no seu `~/.config`.

## Licença

MIT. Veja [LICENSE](LICENSE).
