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

### Backends disponíveis

| Etapa | Com `GROQ_API_KEY` (recomendado) | Sem `GROQ_API_KEY` (fallback local) |
|---|---|---|
| ASR | Groq `whisper-large-v3-turbo` (~0,1–0,3 s/frase) | `faster-whisper` CPU (~4–8 s/frase) |
| Tradução PT→ES | Groq LLM `openai/gpt-oss-20b` (~0,3–0,7 s) | NLLB-200 CPU (~0,5–2 s) |
| VAD | `EnergyDetector` (sem torch) ou Silero (com torch) | idem |
| Latência total | ~5–7 s | ~10–15 s |

### Como a sincronização funciona

O reconhecimento só pode começar quando uma frase termina, então a legenda sempre chega **depois**
da fala. O player é atrasado de propósito (`Atraso do vídeo`, padrão 10 s) para dar tempo.

* `capture.py` mede, para cada trecho de áudio, *quando* ele estava na borda ao vivo
  (`AudioClock`: filtro de mínimo, porque o HLS entrega o áudio em blocos de um segmento inteiro).
* O servidor envia cada legenda com `age_start` / `age_end` = "há quantos segundos, na borda ao vivo,
  esta fala começou / terminou". Idades em vez de horários: relógios do servidor e do navegador
  não precisam coincidir.
* O navegador mostra a legenda após `latência_do_player − age_start` segundos
  (`static/captions.js`). Se já passou do tempo, mostra na hora e **conta como atrasada**; a barra
  de status avisa quando mais de 20 % chegam atrasadas → aumente o atraso do vídeo.
* `Ajuste das legendas ±0,5 s` corrige a diferença constante entre o que o ffmpeg e o HLS.js
  consideram "ao vivo" (depende do canal). Os dois valores ficam salvos no navegador.

### Quanto atraso o vídeo precisa?

O que o atraso tem de cobrir (por legenda): duração da frase + 0,5 s de silêncio (`VAD_SILENCE_MS`)
+ até um segmento HLS inteiro de "jitter" + tempo do ASR + tempo da tradução.
Cada legenda registra isso no log:
```
caption ready: speech started 7.4s ago … (seg 5.1s, ASR 0.7s, translation 0.7s)
```
**1–2 s não bastam** com este desenho (reconhecimento por frases); espere 8–12 s com Groq,
10–15 s sem. Veja `LATENCY_PROBLEM.md` para guia completo.

## Instalação rápida (com Groq)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
ffmpeg -version          # ffmpeg precisa estar instalado no sistema
cp .env.example .env
# Edite .env e preencha GROQ_API_KEY com a chave de https://console.groq.com/keys
python main.py           # abre http://localhost:8000
```

Ordem recomendada na primeira vez:
1. `python main.py --capture-only` — veja o texto PT no terminal e as idades (`age`).
2. Ative a tradução e avalie o ES.
3. Abra o player e ajuste *Atraso do vídeo* / *Ajuste das legendas*.

## Instalação sem Groq (modelos locais)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt   # inclui torch (~800 MB) e faster-whisper
cp .env.example .env
# Deixe GROQ_API_KEY em branco — o sistema usa faster-whisper + NLLB automaticamente
python main.py
```

## Configuração (`.env` — veja `.env.example`)

### Groq (recomendado)

| Variável | Padrão | Descrição |
|---|---|---|
| `GROQ_API_KEY` | — | Chave da API. Quando preenchida, ativa Groq para ASR e tradução. Obtenha em https://console.groq.com/keys |
| `GROQ_TRANSLATE_MODEL` | `openai/gpt-oss-20b` | Modelo LLM para PT→ES. Veja modelos disponíveis com `groq.models.list()` |

### Stream

| Variável | Padrão | Descrição |
|---|---|---|
| `STREAM_URL` | URL da TV Cidade 10 | Stream HLS de entrada |
| `PROXY_ALLOWED_HOSTS` | — | Hosts extras que o proxy pode acessar. `.dominio.com` = subdomínios |

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

### Tradução local (ignorado quando `GROQ_API_KEY` está preenchido)

| Variável | Padrão | Descrição |
|---|---|---|
| `ENABLE_TRANSLATION` | `true` | `false` = só PT, sem ES |
| `TRANSLATION_BACKEND` | `nllb` | `groq` \| `nllb` \| `marian` (via inglês, não testado) \| `llm` |
| `TRANSLATION_DEVICE` | `auto` | `auto` \| `cpu` \| `cuda` |

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

## Segurança do proxy

O proxy HLS só serve o `STREAM_URL` configurado; toda URL reescrita leva uma assinatura HMAC com
segredo aleatório por execução; e cada requisição de saída (inclusive redirecionamentos) só pode ir
para o host do stream ou para `PROXY_ALLOWED_HOSTS`. Se o canal usar outro host para os segmentos e
o player mostrar 403/502, adicione-o em `PROXY_ALLOWED_HOSTS`.

## Testes

```bash
pip install pytest websockets
python -m pytest tests -q                 # unitários: relógio, VAD, legendas, proxy/SSRF, WebSocket
node tests/captions.test.js               # agendador de legendas
python tests/e2e_local_hls.py             # ffmpeg real → stream HLS local → captura/VAD/legendas (~30 s)
python tests/smoke_main.py                # main.py + servidor + WebSocket + Ctrl+C (modelos simulados)
npm i jsdom && node tests/page.test.js    # lógica da página (HLS.js/WebSocket simulados)
```

**Não coberto pelos testes** (precisa do seu ambiente): Whisper e NLLB reais, o stream real da
TV Cidade 10, e HLS.js num navegador de verdade.

## Riscos conhecidos

* Áudio com música/ruído/vários locutores é o pior caso para ASR; o Whisper pode inventar texto
  (há filtros de alucinação em `transcribe.py`, mas não são perfeitos).
* Se o pipeline for mais lento que o tempo real, o log avisa (`falling behind`) e legendas antigas
  são descartadas. Use um modelo menor, Groq, ou GPU.
* Quando o stream está fora do ar, o sistema tenta reconectar com backoff exponencial
  (2 s → 4 → 8 → … → 60 s); o log mostra `ffmpeg error: Connection refused` e o intervalo atual.
