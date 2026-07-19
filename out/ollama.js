"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.normaliseBaseUrl = normaliseBaseUrl;
exports.isOpenAICompat = isOpenAICompat;
exports.autoGrammarMode = autoGrammarMode;
exports.isRunning = isRunning;
exports.listModels = listModels;
exports.pull = pull;
exports.generate = generate;
exports.stripThinking = stripThinking;
exports._isTransientGenerateError = _isTransientGenerateError;
exports.setRpmLimit = setRpmLimit;
exports.calibrate = calibrate;
// ollama.ts — provider-aware HTTP client for Ollama, LM Studio, OpenAI, Anthropic
const http = require("http");
const https = require("https");
const url_1 = require("url");
// ── Thinking-model support ──────────────────────────────────────────────────
// Some models/providers (Qwen3-style "thinking" models in particular) can
// prepend a <think>...</think> block of chain-of-thought reasoning before
// the actual answer. Whether this appears is NOT under this client's
// control per-provider -- it depends on the model, the endpoint's own
// default, and whatever system prompt the caller sent -- so this can't be
// solved by only handling one provider's response shape. What CAN be done
// centrally, regardless of which of _generateOnce's 7 return paths produced
// the text, is: check whether a <think> block is actually present, and if
// so strip it; if not, pass the text through completely unchanged. That
// check-then-strip-or-passthrough is deliberately NOT unconditional
// stripping, so a model/provider that never emits <think> at all sees zero
// behavior change.
const THINK_BLOCK_RE = /<think>[\s\S]*?<\/think>\s*/gi;
const UNCLOSED_THINK_RE = /<think>[\s\S]*$/i;
function stripThinking(text) {
    if (!text || !/<think>/i.test(text)) {
        // No thinking tag present at all -- do the same thing: return the
        // text exactly as received, no-op.
        return text;
    }
    let out = text.replace(THINK_BLOCK_RE, '');
    // A <think> with no matching </think> means generation was cut off
    // (max_tokens, a stop sequence, a dropped stream) while still inside
    // the reasoning block, before any real answer was produced. Trim that
    // dangling fragment too, rather than handing an unterminated <think>
    // tag to a caller (Build's normalizer/parser, Plan/Review's own
    // parsing) that has no idea what to do with it.
    out = out.replace(UNCLOSED_THINK_RE, '');
    return out.trim();
}
// ── RPM throttling ────────────────────────────────────────────────────────────
// Simple sliding-window limiter, keyed per (channel + provider + baseUrl).
// FIX: the key used to be just (provider + baseUrl). That's fine when Plan/
// Review and Build point at different endpoints, but the moment BOTH are
// pointed at the same provider+baseUrl (e.g. Plan/Review AND Build both set
// to NVIDIA NIM), they collapsed onto the exact same bucket — so two 20 RPM
// sliders didn't add up to 40 RPM of actual allowance, they silently shared
// ONE 20 RPM budget across all three stages combined. 'channel' ('plan-
// review' vs 'build') keeps them independent regardless of whether the
// underlying endpoint happens to be the same, matching what the two
// separate sliders in the settings UI actually imply. This exists
// specifically for cloud endpoints (e.g. NVIDIA NIM) with a hard
// requests-per-minute ceiling: without it, a busy retry/fallback loop can
// burst well past the provider's limit and get 429s. 0 or unset means
// "no limit" (the previous, unthrottled behavior).
const _rpmLimits = new Map(); // key -> requests/min
const _rpmWindows = new Map(); // key -> array of request timestamps (ms)
function _rpmKey(channel, provider, baseUrl) {
    return `${channel}::${provider}::${baseUrl}`;
}
/** Configure (or clear, with limit=0) the RPM ceiling for a given channel+provider+baseUrl combination. */
function setRpmLimit(channel, provider, baseUrl, limit) {
    const key = _rpmKey(channel, provider, baseUrl);
    if (!limit || limit <= 0) {
        _rpmLimits.delete(key);
        _rpmWindows.delete(key);
    }
    else {
        _rpmLimits.set(key, limit);
    }
}
/** Wait, if necessary, until a request is allowed under the configured RPM limit. Resolves immediately if no limit is set. */
async function _waitForRpmSlot(channel, provider, baseUrl) {
    const key = _rpmKey(channel, provider, baseUrl);
    const limit = _rpmLimits.get(key);
    if (!limit)
        return;
    const WINDOW_MS = 60000;
    let timestamps = _rpmWindows.get(key) || [];
    for (;;) {
        const now = Date.now();
        timestamps = timestamps.filter(t => now - t < WINDOW_MS);
        if (timestamps.length < limit) {
            timestamps.push(now);
            _rpmWindows.set(key, timestamps);
            return;
        }
        // Oldest request in the window determines how long until a slot frees up.
        const waitMs = WINDOW_MS - (now - timestamps[0]) + 25; // +25ms safety margin
        await new Promise(resolve => setTimeout(resolve, Math.max(waitMs, 50)));
    }
}
// ── URL normalisation ─────────────────────────────────────────────────────────
function normaliseBaseUrl(raw) {
    return raw.trim().replace(/\/v1\/?$/, '').replace(/\/$/, '');
}
function getPort(parsed) {
    if (parsed.port)
        return Number(parsed.port);
    return parsed.protocol === 'https:' ? 443 : 80;
}
function isOpenAICompat(provider) {
    return ['openai', 'openai-compat', 'lmstudio', 'anthropic'].includes(provider);
}
function autoGrammarMode(provider) {
    // CORRECTION: GBNF was previously emitted for Ollama, on the assumption
    // that Ollama's /api/generate `grammar` field did arbitrary-GBNF
    // passthrough. It doesn't — Ollama only supports JSON-Schema-derived
    // structured output via its `format` field, and the raw `grammar` field
    // this client used to send was silently ignored by the server. Real
    // GBNF grammar sampling, on the model actually used by Build, only
    // works against KoboldCpp (a llama.cpp-based server) here. Every other
    // provider — including Ollama and LM Studio — correctly falls back to
    // tool-calling/function-calling constrained schemas instead of sending
    // a grammar field that does nothing.
    return provider === 'koboldcpp' ? 'gbnf' : 'tools';
}
function buildHeaders(apiKey, extra, provider) {
    const h = { 'Content-Type': 'application/json' };
    if (apiKey) {
        if (provider === 'anthropic') {
            // Anthropic's API authenticates via x-api-key + anthropic-version,
            // NOT "Authorization: Bearer". The previous implementation only
            // ever set Bearer auth here, which means every prior call to
            // Anthropic — streaming or not — would have failed with a 401,
            // independent of anything related to streaming support itself.
            h['x-api-key'] = apiKey;
            h['anthropic-version'] = '2023-06-01';
        }
        else {
            h['Authorization'] = `Bearer ${apiKey}`;
        }
    }
    if (extra)
        Object.assign(h, extra);
    return h;
}
function httpRequest(url, method, body, apiKey, provider, timeoutMs = 8000) {
    return new Promise((resolve, reject) => {
        const parsed = new url_1.URL(url);
        const lib = parsed.protocol === 'https:' ? https : http;
        const bodyStr = body ? JSON.stringify(body) : undefined;
        const headers = buildHeaders(apiKey, undefined, provider);
        if (bodyStr)
            headers['Content-Length'] = String(Buffer.byteLength(bodyStr));
        const req = lib.request({
            hostname: parsed.hostname,
            port: getPort(parsed),
            path: parsed.pathname + parsed.search,
            method, headers
        }, (res) => {
            let data = '';
            res.on('data', (chunk) => data += chunk);
            res.on('end', () => {
                if (res.statusCode >= 400)
                    reject(new Error(`HTTP ${res.statusCode}: ${data.substring(0, 300)}`));
                else
                    resolve(data);
            });
        });
        req.on('error', reject);
        // FIX: was a flat 8000ms for every call regardless of payload size.
        // Review and L5-fallback calls carry the full generated code in the
        // prompt and routinely need much longer than 8s for a real model to
        // respond — the old fixed timeout aborted those before the model
        // had a realistic chance to finish, surfacing as "Review error:
        // timeout" / "L5 fallback error: timeout" even when nothing was
        // actually broken on the model side. Callers now pass a timeout
        // sized to their payload (see generate()'s isLargePayload check).
        req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('timeout')); });
        if (bodyStr)
            req.write(bodyStr);
        req.end();
    });
}
function streamRequest(url, body, apiKey, onChunk, extraHeaders, provider, timeoutMs = 180000) {
    return new Promise((resolve, reject) => {
        const parsed = new url_1.URL(url);
        const lib = parsed.protocol === 'https:' ? https : http;
        const bodyStr = JSON.stringify(body);
        const headers = buildHeaders(apiKey, extraHeaders, provider);
        headers['Content-Length'] = String(Buffer.byteLength(bodyStr));
        const req = lib.request({
            hostname: parsed.hostname,
            port: getPort(parsed),
            path: parsed.pathname,
            method: 'POST', headers
        }, (res) => {
            if (res.statusCode >= 400) {
                let errBuf = '';
                res.on('data', (c) => errBuf += c);
                res.on('end', () => reject(new Error(`HTTP ${res.statusCode}: ${errBuf.substring(0, 300)}`)));
                return;
            }
            let buf = '';
            res.on('data', (chunk) => {
                buf += chunk.toString();
                const lines = buf.split('\n');
                buf = lines.pop() ?? '';
                for (const rawLine of lines) {
                    const line = rawLine.trim();
                    if (!line)
                        continue;
                    // SSE format (OpenAI/Anthropic): "data: {...}" or "event: ..." lines.
                    // Ollama's /api/generate and /api/chat use raw newline-delimited JSON
                    // with no "data: " prefix at all — handle both by stripping the
                    // prefix only when present, and ignoring SSE "event:" lines (the
                    // payload we need is always on the following "data:" line).
                    if (line.startsWith('event:'))
                        continue;
                    const payload = line.startsWith('data:') ? line.slice(5).trim() : line;
                    if (payload === '[DONE]')
                        continue; // OpenAI's literal stream terminator, not JSON
                    onChunk(payload);
                }
            });
            res.on('end', () => {
                if (buf.trim()) {
                    const line = buf.trim();
                    const payload = line.startsWith('data:') ? line.slice(5).trim() : line;
                    if (payload && payload !== '[DONE]')
                        onChunk(payload);
                }
                resolve();
            });
        });
        req.on('error', reject);
        // FIX: this was a flat 180000ms regardless of provider/payload,
        // completely independent of the provider/payload-aware timeoutMs
        // generate() computes below (8s/60s/90s/180s tiers, including the
        // CPU-only KoboldCpp floor). Streamed calls — which is every Build
        // chunk — got the flat 180s ceiling no matter what, while the
        // calibrated tier generate() worked out never actually reached the
        // network layer for them. Now honors whatever the caller passes,
        // still defaulting to 180000 for any caller that doesn't supply one.
        req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('stream timeout')); });
        req.write(bodyStr);
        req.end();
    });
}
async function isRunning(baseUrl, provider = 'ollama', apiKey) {
    try {
        const base = normaliseBaseUrl(baseUrl);
        // FIX: koboldcpp previously fell through to the Ollama-shaped
        // /api/tags branch below (isOpenAICompat('koboldcpp') is false).
        // KoboldCpp DOES implement an Ollama-compatibility shim that may
        // respond on /api/tags too, but its own docs explicitly say that
        // shim "is NOT recommended for normal usage" — relying on it here
        // means isRunning() could return true via a deprecated/discouraged
        // compatibility path instead of the real native endpoint. The
        // documented, recommended endpoint for checking server/model state
        // is /api/v1/model.
        const url = provider === 'koboldcpp' ? `${base}/api/v1/model`
            : isOpenAICompat(provider) ? `${base}/v1/models` : `${base}/api/tags`;
        await httpRequest(url, 'GET', undefined, apiKey, provider);
        return true;
    }
    catch {
        return false;
    }
}
async function listModels(baseUrl, provider = 'ollama', apiKey) {
    const base = normaliseBaseUrl(baseUrl);
    try {
        // FIX: same issue as isRunning() — koboldcpp fell through to the
        // Ollama-shaped /api/tags branch and parsed the response as
        // { models: [{ name }] }, which is the wrong shape for KoboldCpp's
        // native API and would silently return an empty list (or garbage,
        // if the Ollama-compat shim happens to respond) rather than a real
        // error. /api/v1/model returns the single currently-loaded model
        // (KoboldCpp/the underlying llama.cpp server load one model at a
        // time, unlike Ollama which can list many available models) —
        // historically as a {"result": "<name>"} shape inherited from the
        // original KoboldAI API. Checking a couple of plausible key names
        // defensively here since this should be confirmed against a real
        // running KoboldCpp instance rather than assumed from one source.
        if (provider === 'koboldcpp') {
            const raw = await httpRequest(`${base}/api/v1/model`, 'GET', undefined, apiKey, provider);
            const data = JSON.parse(raw);
            const name = data.result ?? data.model ?? data.name;
            return name ? [name] : [];
        }
        if (isOpenAICompat(provider)) {
            // Anthropic's /v1/models is a real, documented endpoint
            // (https://docs.anthropic.com/en/api/models-list) returning the
            // same { data: [{ id, ... }] } shape OpenAI uses — it does NOT
            // need a separate hardcoded branch. The previous implementation
            // special-cased 'anthropic' with a static list that was both
            // unnecessary (the live endpoint works once buildHeaders sends
            // the correct x-api-key/anthropic-version headers) and stale
            // (pinned to the 4-5 generation against the actual current
            // 4.6/4.7/4.8 lineup).
            const raw = await httpRequest(`${base}/v1/models`, 'GET', undefined, apiKey, provider);
            const data = JSON.parse(raw);
            const ids = (data.data ?? []).map((m) => m.id).filter(Boolean).sort();
            if (ids.length > 0)
                return ids;
            if (provider === 'anthropic')
                return _anthropicFallbackModels();
            return ids;
        }
        const raw = await httpRequest(`${base}/api/tags`, 'GET');
        const data = JSON.parse(raw);
        return (data.models ?? []).map((m) => m.name).sort();
    }
    catch {
        // Live call failed entirely (network error, bad key, etc.) — for
        // Anthropic specifically, fall back to a known-good static list
        // rather than returning nothing, since model IDs are still useful
        // to show even if the live fetch couldn't be verified.
        if (provider === 'anthropic')
            return _anthropicFallbackModels();
        return [];
    }
}
/**
 * Last-resort static model list, used only if a live /v1/models call to
 * Anthropic fails outright. Kept current as of this writing — but being a
 * hardcoded list, it WILL drift again over time the same way the previous
 * one did. The live call above is the real source of truth; this is purely
 * a degraded-mode fallback so the dropdown isn't empty.
 */
function _anthropicFallbackModels() {
    return [
        'claude-opus-4-8', 'claude-opus-4-7', 'claude-opus-4-6',
        'claude-sonnet-4-6', 'claude-haiku-4-5',
    ];
}
async function pull(baseUrl, model, onProgress) {
    const base = normaliseBaseUrl(baseUrl);
    await streamRequest(`${base}/api/pull`, { name: model, stream: true }, undefined, line => {
        try {
            const obj = JSON.parse(line);
            if (obj.total && obj.completed)
                onProgress(Math.round((obj.completed / obj.total) * 100));
        }
        catch { /* ignore */ }
    });
}
async function _generateOnce(opts) {
    const { baseUrl, model, provider = 'ollama', apiKey, system, prompt, messages, grammar, stream, onToken, maxTokens, responseFormat, channel = 'plan-review' } = opts;
    const base = normaliseBaseUrl(baseUrl);
    // RPM throttling — waits here (before any network call) if this
    // channel+provider+baseUrl combination has a configured limit and is
    // currently at capacity. No-op if setRpmLimit() was never called for
    // this key. 'channel' defaults to 'plan-review' for any caller that
    // doesn't pass one, matching prior behavior for anything not yet
    // updated to pass it explicitly.
    await _waitForRpmSlot(channel, provider, base);
    const compat = isOpenAICompat(provider);
    // FIX: timeout used to be a flat 8000ms for every call. Review and
    // L5-fallback calls carry the full generated code (look for the
    // "Generated code:" marker SKILL_REVIEW.md's prompt always includes),
    // which routinely takes much longer than 8s for any real model —
    // especially a cloud provider — to read and respond to. Plan's calls
    // and Build's per-attempt calls stay on the short default since their
    // prompts are comparatively small.
    const payloadText = [system, prompt, ...(messages || []).map(m => m.content)]
        .filter(Boolean).join('\n');
    const isLargePayload = payloadText.includes('Generated code:') || payloadText.length > 4000;
    let timeoutMs = isLargePayload ? 90000 : 8000;
    // KoboldCpp on a CPU-only host decodes far slower than a GPU-backed
    // provider — the 8s/90s defaults above were tuned assuming GPU
    // inference and will time out mid-generation here. Floor at 60s for
    // small prompts, 180s for large ones (Review-sized payloads).
    if (provider === 'koboldcpp') {
        timeoutMs = isLargePayload ? 180000 : 60000;
    }
    if (provider === 'koboldcpp') {
        // KoboldCpp's native API, not OpenAI-compatible. /api/v1/generate
        // takes a flat {prompt, grammar} body (grammar is a raw GBNF
        // string, same format as the .gbnf files on disk) and returns
        // {results: [{text: "..."}]}. The streaming counterpart is a
        // separate endpoint, /api/extra/generate/stream, emitting SSE
        // lines shaped {token: "..."} rather than the OpenAI/Ollama
        // {response: "..."} or {choices: [...]} shapes streamRequest's
        // callers elsewhere expect — handled inline below rather than
        // forcing a shared shape on an unrelated API.
        const fullPrompt = system ? `${system}\n\n${prompt ?? ''}` : (prompt ?? '');
        // FIX: max_length was hardcoded to 512 — roughly 8x smaller than
        // settings.js's configured maxTokens default (4096), and far too
        // small for a real Build response generating a full multi-shape,
        // multi-action Dictum program. This silently truncated anything
        // beyond a trivial program. Now reads the same maxTokens the
        // OpenAI-compatible path already respects (see max_tokens below),
        // falling back to 4096 if the caller didn't pass one.
        const body = { prompt: fullPrompt, max_length: maxTokens || 4096 };
        if (grammar)
            body.grammar = grammar;
        if (!stream || !onToken) {
            const raw = await httpRequest(`${base}/api/v1/generate`, 'POST', body, apiKey, provider, timeoutMs);
            const data = JSON.parse(raw);
            return data.results?.[0]?.text ?? '';
        }
        let full = '';
        await streamRequest(`${base}/api/extra/generate/stream`, body, apiKey, line => {
            try {
                const obj = JSON.parse(line);
                const piece = obj.token;
                if (piece) {
                    full += piece;
                    onToken(piece);
                }
            }
            catch { /* ignore non-JSON keepalive lines */ }
        }, undefined, provider, timeoutMs);
        return full;
    }
    if (compat) {
        const msgs = [];
        if (system)
            msgs.push({ role: 'system', content: system });
        if (messages) {
            // Filter duplicate system messages if we already pushed one (fix issue 10)
            const filtered = system ? messages.filter(m => m.role !== 'system') : messages;
            msgs.push(...filtered);
        }
        else if (prompt)
            msgs.push({ role: 'user', content: prompt });
        const wantsStream = !!(stream && onToken);
        const body = { model, messages: msgs, stream: wantsStream, temperature: 0.2 };
        // Anthropic's Messages API doesn't take an OpenAI-shaped
        // response_format field at all, and NIM/vLLM's structured-output
        // support is explicitly documented as model-dependent — this is
        // deliberately opt-in per-call (see toolSchema.js's caller in
        // extension.js, which catches an unsupported-response_format
        // failure and retries once without it) rather than assumed safe
        // for every provider/model combination.
        if (responseFormat && provider !== 'anthropic') {
            body['response_format'] = responseFormat;
        }
        // FIX: max_tokens was ONLY ever set for provider === 'anthropic'
        // (hardcoded 4096). Every other OpenAI-compatible provider —
        // OpenRouter, NIM, Groq, LM Studio, vLLM — got NO max_tokens field
        // on the request at all, so the provider fell back to whatever ITS
        // own default ceiling is for that model (65536+ for some). That's
        // the real cause of "requested up to 65536 tokens, can only afford
        // 10509" (402) and "Requested 85942, Limit 8000 TPM" (413) errors —
        // not insufficient credit, just Dictum never telling the API how
        // many tokens this call actually needs. Now always sent, using the
        // caller's maxTokens (Build already passes cfg.maxTokens; Plan/
        // Review now do too — see extension.js) with the same 4096 fallback
        // anthropic already had.
        body['max_tokens'] = maxTokens || 4096;
        const url = provider === 'anthropic' ? `${base}/v1/messages` : `${base}/v1/chat/completions`;
        if (!wantsStream) {
            const raw = await httpRequest(url, 'POST', body, apiKey, provider, timeoutMs);
            const data = JSON.parse(raw);
            if (provider === 'anthropic')
                return data.content?.[0]?.text ?? '';
            return data.choices?.[0]?.message?.content ?? '';
        }
        // Streaming path. OpenAI and Anthropic use genuinely different SSE
        // payload shapes — previously this branch hardcoded stream:false
        // unconditionally, so cloud providers always blocked silently
        // until the full response arrived with no progress signal.
        let full = '';
        await streamRequest(url, body, apiKey, line => {
            try {
                const obj = JSON.parse(line);
                if (provider === 'anthropic') {
                    // Only content_block_delta events of type text_delta carry
                    // visible text. Other event types (message_start,
                    // content_block_start, ping, message_delta, etc.) are
                    // structural and must be ignored rather than mined for
                    // a .text field that doesn't exist on them.
                    if (obj.type === 'content_block_delta' && obj.delta?.type === 'text_delta') {
                        const piece = obj.delta.text ?? '';
                        if (piece) {
                            full += piece;
                            onToken(piece);
                        }
                    }
                }
                else {
                    // OpenAI Chat Completions streaming: choices[0].delta.content.
                    // delta can be {} (role-only or final empty chunk) with no
                    // content field at all — guard rather than assume presence.
                    const piece = obj.choices?.[0]?.delta?.content;
                    if (piece) {
                        full += piece;
                        onToken(piece);
                    }
                }
            }
            catch { /* non-JSON line (shouldn't happen after [DONE]/event: filtering, but don't crash the stream over one bad line) */ }
        }, undefined, provider, timeoutMs);
        return full;
    }
    if (messages) {
        const body = { model, messages: messages, stream: false };
        const raw = await httpRequest(`${base}/api/chat`, 'POST', body, undefined, provider, timeoutMs);
        const data = JSON.parse(raw);
        return data.message?.content ?? data.response ?? '';
    }
    const body = { model, prompt: prompt ?? '', stream: stream ?? false };
    if (system)
        body.system = system;
    if (grammar)
        body.grammar = grammar;
    if (!stream || !onToken) {
        const raw = await httpRequest(`${base}/api/generate`, 'POST', body, undefined, provider, timeoutMs);
        const data = JSON.parse(raw);
        return data.response ?? '';
    }
    let full = '';
    await streamRequest(`${base}/api/generate`, body, undefined, line => {
        try {
            const obj = JSON.parse(line);
            if (obj.response) {
                full += obj.response;
                onToken(obj.response);
            }
        }
        catch { /* ignore */ }
    }, undefined, provider, timeoutMs);
    return full;
}
// Free-tier / shared cloud endpoints (OpenRouter's :free models, NVIDIA
// NIM's free tier, etc.) routinely return a narrow set of "try again"
// conditions under load: 429 (rate limited), 503 (ResourceExhausted / all
// workers busy), or a raw connection drop/timeout. These are NOT the same
// as a real problem with the request (bad model name, invalid key,
// malformed body, insufficient credit, request-too-large) — retrying THOSE
// would just fail identically every time. This wrapper retries only the
// narrow transient set, with exponential backoff, so a request that would
// have succeeded a few seconds later doesn't surface as a hard failure to
// Plan/Build/Review on the first blip.
// FIX: backoff used to be 500ms/1s/2s (500 * 2^attempt) — too fast for an
// upstream capacity issue (e.g. NVIDIA NIM's shared free-tier worker pool
// being momentarily exhausted) to actually clear before the next attempt.
// Bumped to a flat 2s/4s/6s schedule instead, giving the upstream more real
// time to free a slot between attempts.
const TRANSIENT_RETRY_DELAYS_MS = [2000, 4000, 6000];
const TRANSIENT_RETRY_LIMIT = TRANSIENT_RETRY_DELAYS_MS.length;
function _isTransientGenerateError(err) {
    const msg = (err && err.message) || '';
    const httpMatch = msg.match(/^HTTP (\d+):/);
    if (httpMatch) {
        const status = Number(httpMatch[1]);
        // 429 = rate limited, 503 = ResourceExhausted/Service Unavailable —
        // both are "try again shortly", not "this request is wrong".
        // Deliberately NOT included: 400 (bad request / degraded function),
        // 402 (insufficient credit), 413 (request too large) — retrying
        // those would fail identically every time; they need a different
        // model/provider or a smaller request, not a retry.
        return status === 429 || status === 503;
    }
    if (msg === 'timeout' || msg === 'stream timeout')
        return true;
    const code = err && err.code;
    return code === 'ECONNRESET' || code === 'ETIMEDOUT' || code === 'ECONNREFUSED';
}
function _sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
async function generate(opts) {
    let lastErr;
    for (let attempt = 0; attempt <= TRANSIENT_RETRY_LIMIT; attempt++) {
        try {
            const raw = await _generateOnce(opts);
            // Single choke point: every provider path in _generateOnce (7
            // different return statements across koboldcpp/OpenAI-compat/
            // Ollama, streamed and non-streamed) funnels through here before
            // any caller (Build, Plan, Review, L5 fallback) sees the text.
            // Fixing it once here means no per-provider branch can forget it,
            // and no caller needs to remember to call it themselves.
            return stripThinking(raw);
        }
        catch (e) {
            lastErr = e;
            if (!_isTransientGenerateError(e)) {
                // Not a "try again" condition (bad request, bad key, request
                // too large, etc.) — fail immediately, and don't offer a
                // manual retry either since retrying would just fail the
                // same way again.
                throw e;
            }
            if (attempt === TRANSIENT_RETRY_LIMIT) {
                // Genuinely transient (429/503/connection drop), but we've
                // now exhausted every automatic retry. Tag the error so the
                // caller can offer a manual "Retry" action instead of just
                // surfacing a dead-end failure — this is exactly the class
                // of error where the person clicking Retry a few seconds
                // later has a real chance of succeeding (e.g. an upstream
                // shared worker pool freeing up a slot).
                e.retriesExhausted = true;
                throw e;
            }
            await _sleep(TRANSIENT_RETRY_DELAYS_MS[attempt]);
        }
    }
    throw lastErr;
}
/**
 * Measures real generation throughput (tokens/sec) against the configured
 * Build provider, so chunk sizing can be based on this specific machine's
 * actual measured speed instead of a fixed guess. CPU-only KoboldCpp on
 * modest hardware and a GPU-backed cloud provider can differ by an order
 * of magnitude or more — a fixed chunk-size constant would either be
 * needlessly conservative (small model, fast host) or still too large
 * (slow CPU-only host), so this exists to remove the guess entirely.
 *
 * Sends a small, fixed-length generation request (no grammar — grammar
 * changes decode speed, and Build's real chunks may or may not use it, so
 * an unconstrained baseline is the more general measurement) and times
 * only the generation itself.
 *
 * Returns tokensPerSecond: 0 on failure (caller should fall back to a
 * conservative default chunk budget rather than treating 0 as "instant").
 */
async function calibrate(opts) {
    const { baseUrl, model, provider = 'ollama', apiKey, sampleTokens = 128 } = opts;
    const prompt = 'Count from one to fifty, writing each number as a word on its own line, ' +
        'and nothing else.';
    const start = Date.now();
    try {
        let text = '';
        await generate({
            baseUrl, model, provider, apiKey,
            prompt, stream: false, maxTokens: sampleTokens,
        }).then(r => { text = r; });
        const elapsedMs = Date.now() - start;
        if (elapsedMs <= 0)
            return { tokensPerSecond: 0, sampleTokens: 0, elapsedMs, error: 'zero elapsed time' };
        // No real tokenizer available client-side — approximate using the
        // same conservative chars-per-token ratio chunking.js uses for
        // budget estimation, so calibration and budgeting stay consistent
        // with each other even though neither is an exact token count.
        const approxTokens = Math.max(1, Math.round(text.length / 3.5));
        const tokensPerSecond = approxTokens / (elapsedMs / 1000);
        return { tokensPerSecond, sampleTokens: approxTokens, elapsedMs, error: null };
    }
    catch (e) {
        return { tokensPerSecond: 0, sampleTokens: 0, elapsedMs: Date.now() - start, error: e.message };
    }
}
//# sourceMappingURL=ollama.js.map