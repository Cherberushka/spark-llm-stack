#!/usr/bin/env bash
# =============================================================================
# 05-verify.sh — End-to-end smoke tests
# Run as root: sudo bash scripts/05-verify.sh
# Tests: bge-m3 embeddings, Qwen3.8-27B via vLLM (text + thinking-off + vision),
#        LiteLLM gateway routing, web front door + /ops + /gateway.
# =============================================================================
set -uo pipefail

PASS=0; FAILED=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=$((FAILED+1)); }
key()  { grep -oP "(?<=^$1=).*" /etc/spark-llm.env 2>/dev/null || echo; }

VLLM_KEY="$(key VLLM_API_KEY)"; GW_KEY="$(key LITELLM_MASTER_KEY)"
# 224×224 solid-red PNG (224 = 28×8 — clean multiple of the Qwen-VL patch grid;
# merge_size 2 → 14×14 patch pairs). Do NOT shrink below ~112×112: tiny images
# (64×64 and smaller) can be rejected by the vision preprocessor (min-patch /
# smart-resize edge cases) → HTTP 500 or empty content.
PNG_B64="iVBORw0KGgoAAAANSUhEUgAAAOAAAADgCAIAAACVT/22AAABr0lEQVR42u3SAQkAAAgEse9jAtsbzRoKgyU4LtMFZ0UCDAoGxaBgUDAoBgWDYlAwKBgUg4JBMSgYFAyKQcGgGBQMCgbFoGBQDKoCBgWDYlAwKBgUg4JBMSgYFAyKQcGgGBQMCgbFoGBQDAoGBYNiUDAoBlUBg4JBMSgYFAyKQcGgGBQMCgbFoGBQDAoGBYNiUDAoBgWDgkExKBgUg4JBwaAYFAwKBsWgYFAMCgYFg2JQMCgGBYOCQTEoGBSDgkHBoBgUDIpBwaBgUAwKBgWDYlAwKAYFg4JBMSgYFIOCQcGgGBQMikHBoGBQDAoGxaBgUDAoBgWDgkExKBgUg4JBwaAYFAyKQcGgYFAMCgbFoGBQMCgGBYNiUDAoGBSDgkHBoBgUDIpBwaBgUAwKBsWgYFAwKAYFg2JQMCgYFIOCQTEoGBQMikHBoGBQDAoGxaBgUDAoBgWDYlAwKBgUg4JBMSgYFAyKQcGgGBQMCgbFoGBQMCgGBYNiUDAoGBSDgkExKBgUDIpBwaAYFAwKBsWgYFAMCgYFg2JQMCgYFIOCQTEoGBQMikHBoBgUDAoG5aMFO3udEvJePl4AAAAASUVORK5CYII="

echo "=== [1/6] Embeddings (Ollama bge-m3) ==="
DIM="$(curl -s http://127.0.0.1:11434/api/embed -d '{"model":"bge-m3","input":"клинические рекомендации"}' | jq -r '.embeddings[0] | length' 2>/dev/null || echo 0)"
(( DIM > 100 )) && ok "bge-m3 embeddings (dim $DIM)" || bad "bge-m3 failed (systemctl status ollama)"

echo "=== [2/6] Qwen3.8-27B engine :8000 — text (thinking off) + speed ==="
CODE="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health)"
[[ "$CODE" == "200" ]] && ok "vLLM /health = 200" || bad "vLLM /health = $CODE (docker logs vllm-qwen38)"
T0=$(date +%s.%N)
RESP="$(curl -s http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer $VLLM_KEY" -H 'Content-Type: application/json' -d '{
  "model": "qwen3.8-27b",
  "messages": [{"role":"user","content":"Функция fizzbuzz на Python — только код."}],
  "max_tokens": 128, "chat_template_kwargs": {"enable_thinking": false}
}')"
T1=$(date +%s.%N)
TXT="$(echo "$RESP" | jq -r '.choices[0].message.content // empty' | head -c 110)"
TOKS="$(echo "$RESP" | jq -r '.usage.completion_tokens // 0')"
if [[ -n "$TXT" ]]; then
  ok "text: ${TXT}..."
  echo "      ${TOKS} tokens in $(awk -v a=$T0 -v b=$T1 'BEGIN{printf "%.1f", b-a}')s wall"
else
  bad "text completion failed — docker logs vllm-qwen38 | tail -40"
fi

echo "=== [3/6] Qwen3.8-27B vision ==="
RESP="$(curl -s http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer $VLLM_KEY" -H 'Content-Type: application/json' -d '{
  "model": "qwen3.8-27b",
  "messages": [{"role":"user","content":[
    {"type":"text","text":"Dominant color of this image? One word."},
    {"type":"image_url","image_url":{"url":"data:image/png;base64,'"$PNG_B64"'"}}]}],
  "max_tokens": 16, "chat_template_kwargs": {"enable_thinking": false}
}')"
VTXT="$(echo "$RESP" | jq -r '.choices[0].message.content // empty' | head -c 50)"
if [[ -n "$VTXT" ]]; then
  ok "vision works ('$VTXT')"
else
  bad "vision failed — raw response: $(echo "$RESP" | head -c 260)"
  echo "      диагностика: journalctl -u qwen38-llamacpp -n 30 --no-pager | grep -iE 'mmproj|clip|mtmd|image|error'"
  echo "      проверка mmproj: ls -lh /opt/models/qwen3.8-27b/mmproj-F16.gguf  (ждём ~0.9 ГБ)"
fi

echo "=== [4/6] Long-context probe (>2048 tokens — catches tokenizer max_length quirks) ==="
# ~2900-token prompt with a marker at the END. If anything silently truncates
# the context at 2048 (tokenizer_config.json model_max_length quirk, client-side
# limits), the model cannot see the marker and the test fails.
FILLER="$(python3 -c "print('The quick brown fox jumps over the lazy dog. ' * 290)")"
PROMPT="${FILLER} The marker code is MARKER-8F31. Question: what is the marker code? Answer with the code only."
RESP="$(jq -nc --arg p "$PROMPT" '{
  model: "qwen3.8-27b", max_tokens: 16, stream: false,
  messages: [{role:"user", content:$p}],
  chat_template_kwargs: {enable_thinking: false}
}' | curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_KEY" -H 'Content-Type: application/json' -d @-)"
ANS="$(echo "$RESP" | jq -r '.choices[0].message.content // empty')"
PTOK="$(echo "$RESP" | jq -r '.usage.prompt_tokens // 0')"
if [[ "$ANS" == *8F31* ]]; then
  ok "long context works: prompt ${PTOK} tokens, marker recalled ('$ANS')"
else
  bad "long-context probe failed (prompt ${PTOK} tok, answer: '${ANS:0:40}') — проверьте --max-model-len и tokenizer_config (скрипт 03 печатает диагностику)"
fi

echo "=== [5/6] LiteLLM gateway :4000 — one API, model switching ==="
CODE="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4000/health/liveliness)"
[[ "$CODE" == "200" ]] && ok "gateway alive" || bad "gateway /health = $CODE (docker logs spark-litellm)"
R="$(curl -s --max-time 180 http://127.0.0.1:4000/v1/chat/completions -H "Authorization: Bearer $GW_KEY" -H 'Content-Type: application/json' -d '{
  "model":"qwen3.8-27b","max_tokens":16,
  "messages":[{"role":"user","content":"Say OK"}],
  "extra_body":{"chat_template_kwargs":{"enable_thinking":false}}
}')"
A="$(echo "$R" | jq -r '.choices[0].message.content // empty' | head -c 40)"
if [[ -n "$A" ]]; then ok "gateway → qwen3.8-27b: '$A'"
else
  bad "gateway → qwen3.8-27b failed: $(echo "$R" | head -c 160)"
  echo "      частая причина: ufw блокирует docker-мост → хост:8000. Проверка/фикс:"
  echo "      sudo ufw status | grep 8000   # должно быть: ALLOW 172.16.0.0/12 ... 8000"
  echo "      sudo ufw allow from 172.16.0.0/12 to any port 8000 proto tcp"
fi
E="$(curl -s --max-time 60 http://127.0.0.1:4000/v1/embeddings -H "Authorization: Bearer $GW_KEY" -H 'Content-Type: application/json' -d '{"model":"bge-m3","input":"тест"}' | jq -r '.data[0].embedding | length' 2>/dev/null || echo 0)"
(( E > 100 )) && ok "gateway → bge-m3 embeddings (dim $E)" || bad "gateway → bge-m3 embeddings failed"

echo "=== [6/6] Web front door (Caddy + Open WebUI + /ops + /gateway) ==="
for P in "/" "/ops"; do
  CODE="$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1$P")"
  [[ "$CODE" =~ ^(200|307|308)$ ]] && ok "https://localhost$P -> $CODE" || bad "https://localhost$P -> $CODE"
done
CODE="$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/gateway/health/liveliness)"
[[ "$CODE" == "200" ]] && ok "https://localhost/gateway → LiteLLM" || bad "/gateway -> $CODE"

echo
echo "════════════════════════════════════"
echo " RESULT: $PASS passed, $FAILED failed"
echo "════════════════════════════════════"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -n "$IP" ]] && echo " Open https://${IP}/ — first registered user becomes ADMIN."
(( FAILED == 0 )) || exit 1
