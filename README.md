# Projeto: Legendas em Tempo Real — TV Cidade 10

**TV Cidade 10** ([tvcidade10.com.br](https://tvcidade10.com.br/)) transmite ao vivo **sem legendas**
(o stream HLS tem só vídeo H.264 + áudio AAC, `closed_captions=0`). Este projeto gera legendas
com IA a partir do áudio e as mostra sobre o vídeo num player próprio:
**PT (original)** embaixo e **ES (tradução)** em cima.

> Uso pessoal. O servidor reenvia o stream através do proxy embutido; não o exponha na internet.

## Arquitetura

```
HLS ──ffmpeg──► PCM 16 kHz ──► VAD ──► ASR ──────────────► Tradução ──► captions.py
 (capture.py)   + AudioClock   (vad.py)  (transcribe.py)    (translate.py)     │
                                                                                ▼
 navegador ◄── WebSocket {pt, es, age_start, age_end} ◄──────────── server.py (FastAPI)
    └─ HLS.js (atrasado N s do ao vivo, via /proxy/playlist) + captions.js agenda legendas
```

Todas as chamadas Groq passam pelo `groq_client.py`, que mantém um único cliente
HTTP reutilizável (pool de conexões) compartilhado entre ASR e tradução.

### Backends disponíveis

| Etapa | Com `GROQ_API_KEY` (recomendado) | Sem `GROQ_API_KEY` (fallback local) |
|---|---|---|
| ASR | Groq `whisper-large-v3-turbo` (~0,1–0,3 s/frase) | `faster-whisper` CPU (~4–8 s/frase) |
| Tradução PT→ES | Groq LLM `openai/gpt-oss-20b` (~0,3–0,7 s) | NLLB-200 CPU (~0,5–2 s) |
| VAD | Silero (com torch) ou `EnergyDetector` (sem torch, segmentação pior) | idem |
| Latência total medida | ~10–15 s (dominada pelo buffering HLS) | ~10–15 s |

### Como a sincronização funciona

O reconhecimento só pode começar quando uma frase termina, então a legenda sempre chega **depois**
da fala. O player é atrasado de propósito (`Atraso do vídeo`, padrão 10 s) para dar tempo.

* `capture.py` mede, para cada trecho de áudio, *quando* ele estava na borda ao vivo
  (`AudioClock`: filtro de mínimo sobre uma janela deslizante de 90 s, porque o HLS entrega
  o áudio em blocos de um segmento inteiro).
* O servidor envia cada legenda com `age_start` / `age_end` = "há quantos segundos, na borda ao vivo,
  esta fala começou / terminou". Idades em vez de horários: relógios do servidor e do navegador
  não precisam coincidir.
* O navegador mostra a legenda após `latência_do_player − age_start` segundos
  (`static/captions.js`). Se já passou do tempo, mostra na hora e **conta como atrasada**; a barra
  de status avisa quando mais de 20 % chegam atrasadas → aumente o atraso do vídeo.
* `Ajuste das legendas ±0,5 s` corrige a diferença constante entre o que o ffmpeg e o HLS.js
  consideram "ao vivo" (depende do canal). Os dois valores ficam salvos no navegador.

### Quanto atraso o vídeo precisa?

O que o atraso tem de cobrir (por legenda): duração da frase + `VAD_SILENCE_MS` (0,5 s)
+ até um segmento HLS inteiro de "jitter" (~8–10 s) + tempo do ASR + tempo da tradução.
Cada legenda registra isso no log:
```
caption ready: speech started 7.4s ago … (seg 5.1s, ASR 0.3s, translation 0.7s)
```
**1–2 s não bastam** com este desenho (reconhecimento por frases). Medido com Groq: 10–15 s.
Veja `LATENCY_PROBLEM.md` para guia completo.

## Instalação

### Início rápido (recomendado)

```bash
git clone https://github.com/ukoquique-proves/cidadeTV-canal10.git
cd cidadeTV-canal10
./start.sh
```

O script cria o venv, instala as dependências, copia `.env.example → .env` e inicia o app.
Abra http://localhost:8000 no browser.

Pré-requisitos: **Python 3.10+** e **ffmpeg** instalados no sistema.
- Ubuntu/Debian: `sudo apt install ffmpeg`
- macOS: `brew install ffmpeg`

Para usar o Groq (recomendado — muito mais rápido):

```bash
GROQ_API_KEY=gsk_... ./start.sh
```

Chave gratuita em https://console.groq.com/keys. O script a grava no `.env`.

Na segunda execução em diante, basta `./start.sh` — o venv e as dependências já existem.

---

### Manual (passo a passo)

### Groq — instalação mínima (~50 MB, sem torch)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.groq.txt
ffmpeg -version          # ffmpeg precisa estar instalado no sistema
cp .env.example .env
# Edite .env: preencha GROQ_API_KEY com a chave de https://console.groq.com/keys
python main.py           # abre http://localhost:8000
```

Ordem recomendada na primeira vez:
1. `python main.py --capture-only` — veja o texto PT no terminal e as idades (`age`).
2. Ative a tradução e avalie o ES.
3. Abra o player e ajuste *Atraso do vídeo* / *Ajuste das legendas*.

### Modelos locais (sem Groq, ~800 MB+ com torch)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt   # core + torch + faster-whisper + NLLB + Silero
cp .env.example .env
# Deixe GROQ_API_KEY em branco — o sistema usa faster-whisper + NLLB automaticamente
python main.py
```

### Groq + Silero VAD (recomendado se torch já estiver instalado)

```bash
pip install -r requirements.groq.txt
pip install silero-vad   # puxa torch (~800 MB) mas melhora muito a segmentação VAD
```

### Arquivos de requirements

| Arquivo | Instala | Tamanho aprox. |
|---|---|---|
| `requirements.groq.txt` | Core + Groq (sem torch) | ~50 MB |
| `requirements.local.txt` | torch + faster-whisper + NLLB + Silero | ~800 MB |
| `requirements.txt` | Tudo (inclui os dois arquivos acima) | ~850 MB |

## Configuração (`.env` — veja `.env.example`)

### Groq (recomendado)

| Variável | Padrão | Descrição |
|---|---|---|
| `GROQ_API_KEY` | — | Chave da API. Quando preenchida, ativa Groq para ASR e tradução. Obtenha em https://console.groq.com/keys |
| `GROQ_TRANSLATE_MODEL` | `openai/gpt-oss-20b` | Modelo LLM para PT→ES. Suporta `reasoning_effort`. Veja modelos disponíveis com `groq.models.list()` |
| `GROQ_REASONING_EFFORT` | — | gpt-oss only: `off` (desativa raciocínio, mais rápido) \| `low` \| `medium` \| `high` (padrão Groq: medium). Raciocínio adiciona latência e tokens extras. |
| `GROQ_TIMEOUT_S` | `8` | Timeout por requisição à Groq (s). Uma chamada travada para todo o pipeline |
| `GROQ_MAX_RETRIES` | `1` | Retentativas do SDK em 429 / 5xx / erro de rede |

> **Nota:** `gpt-oss-20b` retorna raciocínio em um campo separado `.reasoning`; `max_tokens` conta apenas tokens de saída, não tokens de raciocínio. Com raciocínio habilitado, planeje ~200–300 tokens extras. Deixe `GROQ_REASONING_EFFORT=off` para desabilitar e acelerar.

### Stream

| Variável | Padrão | Descrição |
|---|---|---|
| `STREAM_URL` | URL da TV Cidade 10 | Stream HLS de entrada |
| `PROXY_ALLOWED_HOSTS` | — | Hosts extras que o proxy pode acessar. `.dominio.com` = subdomínios. Pode ficar em branco para este canal. |

### Servidor web

| Variável | Padrão | Descrição |
|---|---|---|
| `HOST` | `127.0.0.1` | `0.0.0.0` expõe o player na sua rede local |
| `PORT` | `8000` | Porta do servidor |
| `LOG_LEVEL` | `INFO` | `DEBUG` mostra mais detalhe |

### ASR local (ignorado quando `GROQ_API_KEY` está preenchido)

| Variável | Padrão | Descrição |
|---|---|---|
| `WHISPER_MODEL` | `small` | `tiny` \| `base` \| `small` \| `medium` \| `large-v3` |
| `WHISPER_DEVICE` | `auto` | `auto` \| `cpu` \| `cuda` |
| `WHISPER_COMPUTE` | `auto` | `auto` \| `int8` (CPU) \| `float16` (CUDA) |
| `WHISPER_BEAM` | `1` | 1 = mais rápido; 2–5 = mais preciso (use com GPU) |
| `WHISPER_PROMPT` | — | Nomes próprios do canal (melhora bastante a precisão) |

### VAD

| Variável | Padrão | Descrição |
|---|---|---|
| `VAD_MAX_SEGMENT_MS` | `5000` | Corte forçado sem pausa (menor = menos latência, mais cortes no meio de frases) |
| `VAD_SILENCE_MS` | `500` | Silêncio que fecha um segmento |
| `VAD_MIN_SPEECH_MS` | `500` | Descarta segmentos menores que isso |
| `VAD_PREROLL_MS` | `300` | Áudio mantido antes do onset detectado |
| `VAD_THRESHOLD` | `0.5` | Limiar do Silero VAD (0–1) |

> **Atenção:** O Silero VAD requer `torch`. Sem ele, o sistema usa `EnergyDetector` (detecção
> por RMS), que segmenta mal em áudio com música/ruído. Se o log avisa que >50% dos segmentos
> são "force-cut" em vez de fechados por silêncio detectado, instale Silero VAD:
> `pip install silero-vad` (~800 MB torch).

### Tradução

| Variável | Padrão | Descrição |
|---|---|---|
| `ENABLE_TRANSLATION` | `true` | `false` = só PT, sem ES |
| `TRANSLATION_BACKEND` | `groq` com chave, senão `nllb` | `groq` \| `nllb` \| `marian` (via inglês, não testado) \| `llm` |
| `TRANSLATION_DEVICE` | `auto` | `auto` \| `cpu` \| `cuda` (só backends locais) |

`ENABLE_TRANSLATION` e `TRANSLATION_BACKEND` valem **com ou sem** `GROQ_API_KEY`.
O backend `llm` genérico requer também `LLM_API_URL`, `LLM_API_KEY` e `LLM_MODEL`.

> **Cota Groq e streams 24/7:** o Groq ASR tem cap generoso (~14.400 req/dia no tier gratuito);
> o tier de LLM para `gpt-oss-20b` é mais restrito (~1.000 req/dia). Num stream contínuo com
> espectadores ativos, a tradução pode bater o limite antes do ASR. Duas estratégias:
>
> 1. **Primeiro: ativar o gate de espectadores** (já habilitado por padrão). Enquanto nenhum
>    cliente WebSocket estiver conectado, nenhuma chamada Groq é feita.
>
> 2. **Se ainda bater a cota:** mova a tradução para local com NLLB. O ASR continua no Groq
>    (rápido, cap alto), só a tradução sai dele:
>    ```bash
>    pip install -r requirements.local.txt   # torch + transformers + NLLB (~800 MB)
>    ```
>    Em `.env`:
>    ```env
>    TRANSLATION_BACKEND=nllb
>    ```
>    Custo: ~0,5–2 s de latência extra por legenda no CPU. Para um stream HLS com 10–15 s de
>    atraso estrutural, é aceitável.

## Opções de linha de comando

```
python main.py                   # pipeline completo + player web
python main.py --capture-only    # imprime transcrições no terminal, sem servidor
python main.py --no-translate    # pula o passo PT→ES
python main.py --fast            # modelo tiny, sem tradução, VAD_MAX_SEGMENT_MS=3000
python main.py --model medium    # substitui WHISPER_MODEL
python main.py --stream-url URL  # substitui STREAM_URL
python main.py --host 0.0.0.0    # expõe na rede local
```

`--fast` define o modelo como `tiny` a menos que `--model` também seja passado; `--model`
tem prioridade.

## Resiliência do stream

Quando o stream cai, `capture.py` usa **backoff exponencial**:

- Primeira falha sem áudio: aguarda 4 s.
- Cada falha seguinte: dobra o intervalo (4 → 8 → 16 → 32 → 60 s).
- Teto de 60 s — continua tentando indefinidamente.
- Se o stream cair no meio de uma sessão (após já ter produzido áudio): volta para 2 s
  (glitch transitório, não uma queda prolongada).
- O stderr do ffmpeg é capturado e logado: `capture — ffmpeg error: Connection refused`.

Quando o upstream retorna **404** (canal fora do ar ou URL mudou), o proxy devolve
`503 Service Unavailable` (em vez de 404) para que o HLS.js continue tentando — um 404
faria o player desistir permanentemente. A página exibe um banner vermelho:
`⚠ Stream offline (404) — o canal está fora do ar. Tentando reconectar…`
O banner some automaticamente quando o stream volta.

### Verificar quando o stream volta

```bash
./check_stream.sh    # imprime HTTP 200 / 404 a cada 60 s até Ctrl+C
```

Se o player do site oficial voltou a funcionar mas o app ainda recebe 404, a URL
do stream provavelmente mudou. Abra o DevTools do browser → aba Network → filtro
`m3u8` → recarregue a página e clique em play → copie a URL da requisição
`playlist.m3u8`. Atualize `STREAM_URL` no `.env` e reinicie.

## Testar sem o canal ao vivo

A pasta `test_hls/` contém um stream HLS curto (60 s de áudio sintético) para
verificar o pipeline sem depender do canal:

```bash
# Terminal 1 — serve o stream local
cd test_hls && python3 -m http.server 9100

# Terminal 2 — inicia o app apontando para o stream local
source venv/bin/activate
STREAM_URL=http://127.0.0.1:9100/stream.m3u8 python main.py
```

O ffmpeg lê o arquivo, o VAD segmenta, o ASR transcreve (via Groq ou local) e as
legendas chegam no browser. O stream termina com `#EXT-X-ENDLIST` e o ffmpeg sai
limpo (`rc=0`); o app reconecta em seguida.

## Segurança do proxy

O proxy HLS só serve o `STREAM_URL` configurado; toda URL reescrita leva uma assinatura HMAC com
segredo aleatório por execução; e cada requisição de saída (inclusive redirecionamentos) só pode ir
para o host do stream ou para `PROXY_ALLOWED_HOSTS`. Se o canal usar outro host para os segmentos e
o player mostrar 403/502, adicione-o em `PROXY_ALLOWED_HOSTS`.

Clientes WebSocket lentos ou travados são desconectados automaticamente após `2 s` sem consumir
mensagens, para que um browser preso não atrrase os outros.

## Testes

```bash
pip install pytest websockets
python -m pytest tests -q                 # unitários: relógio, VAD, legendas, proxy/SSRF, WebSocket
node tests/captions.test.js               # agendador de legendas
python tests/e2e_local_hls.py             # ffmpeg real → stream HLS local → captura/VAD/legendas (~30 s)
python tests/smoke_main.py                # main.py + servidor + WebSocket + Ctrl+C (modelos simulados)
node tests/page.test.js                   # lógica da página (HLS.js/WebSocket SIMULADOS em jsdom)
```

`jsdom` já está instalado em `node_modules/` (instalado com `npm install --save-dev jsdom@24`).
Se a pasta não existir, execute `npm install` primeiro.

Os testes unitários Python não precisam de torch — funcionam com a instalação mínima
(`requirements.groq.txt`).

**Teste com stubs (jsdom, não browser real)**
- `tests/page.test.js` testa a lógica `static/captions.js` (agendador de legendas,
  sincronização de idade, detecção de atraso) com HLS.js e WebSocket mockados via jsdom.
  O comportamento com um player HLS real (HLS.js em Firefox/Chrome) é **não testado**
  — possíveis diferenças em timing, comportamento de buffering, ou tratamento de eventos.

**Não coberto pelos testes** (precisa do seu ambiente):
- Whisper e NLLB reais (modelos são simulados)
- Stream real da TV Cidade 10
- HLS.js em um navegador de verdade (Firefox, Chrome, Safari, etc.)
- Comportamento end-to-end com várias condições de rede e buffering

## Riscos conhecidos

* Áudio com música/ruído/vários locutores é o pior caso para ASR; o Whisper pode inventar texto
  (há filtros de alucinação em `transcribe.py`, mas não são perfeitos).
* Se o pipeline for mais lento que o tempo real, o log avisa (`falling behind`) e legendas antigas
  são descartadas. Use um modelo menor, Groq, ou GPU.
* **Cota Groq (tradução):** no tier gratuito, `gpt-oss-20b` aceita ~1.000 req/dia. Com
  espectadores ativos a tarde toda, isso se esgota. O gate de espectadores (zero chamadas sem
  ninguém assistindo) é a primeira defesa. Se não for suficiente, use `TRANSLATION_BACKEND=nllb`
  para tirar a tradução do Groq — veja a nota em "Tradução" acima.
* O Silero VAD requer `torch`. Sem ele, o sistema usa `EnergyDetector` (detecção por RMS),
  que segmenta por energia sonora e não por pausa de fala. Em áudio com música/ruído, isto
  causa cortes no meio de frases (~5 s). O log alertará se **>50% dos segmentos forem "force-cut"**
  (corte forçado) em vez de fechados naturalmente por silêncio. Quando isso acontecer, instale
  Silero VAD: `pip install silero-vad` (~800 MB torch).
