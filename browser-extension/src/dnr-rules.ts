/**
 * DeclarativeNetRequest rules for intercepting LLM API calls.
 *
 * When installed, the extension redirects requests to known LLM API
 * endpoints to the local ToolRecall forward proxy. The proxy caches
 * responses by request-body hash — identical prompts return cached
 * responses without hitting the real API.
 *
 * Architecture:
 *   Browser → DNR redirect → http://127.0.0.1:8080/{path}
 *     → ForwardProxy checks api_cache
 *     → HIT: respond from cache (0 API cost, 0 tokens)
 *     → MISS: forward to real API, cache response, return
 *
 * The redirect preserves ALL original headers (Authorization, Content-Type)
 * and body — no MITM, no cert installation needed.
 */

/** Known LLM API hosts that should be redirected through the forward proxy */
export const API_HOSTS: string[] = [
  'api.openai.com',
  'api.anthropic.com',
  'generativelanguage.googleapis.com',
  'api.deepseek.com',
  'api.x.ai',
  'api.mistral.ai',
  'api.groq.com',
  'api.together.xyz',
  'openrouter.ai',
];

/** Default local forward proxy port */
const FORWARD_PROXY_PORT = 8080;

/**
 * Build DNR rule IDs deterministically from host index.
 * Rules need unique integer IDs. We allocate a range.
 */
function ruleId(index: number): number {
  return 1000 + index;
}

/**
 * Update DNR rules to redirect API calls through the local forward proxy.
 *
 * Call this after port discovery or when the user changes the proxy port.
 * Removes old rules first, then installs new ones for all API_HOSTS.
 *
 * Requires `declarativeNetRequest` permission in manifest.json.
 *
 * @param port - Local forward proxy port (default: 8080)
 */
export async function updateDnrRules(port: number = FORWARD_PROXY_PORT): Promise<void> {
  // Check API availability
  if (!chrome.declarativeNetRequest) {
    console.warn('[ToolRecall] declarativeNetRequest not available — skipping DNR rules');
    return;
  }

  try {
    // Remove all previous ToolRecall rules (rule IDs 1000-1099)
    const oldRuleIds: number[] = [];
    for (let i = 0; i < API_HOSTS.length; i++) {
      oldRuleIds.push(ruleId(i));
    }
    await chrome.declarativeNetRequest.updateSessionRules({
      removeRuleIds: oldRuleIds,
    });
  } catch {
    // First time — no rules to remove, that's fine
  }

  // Build new rules
  const rules: chrome.declarativeNetRequest.Rule[] = API_HOSTS.map((host, index) => ({
    id: ruleId(index),
    priority: 1,
    action: {
      type: chrome.declarativeNetRequest.RuleActionType.REDIRECT,
      redirect: {
        regexSubstitution: `http://127.0.0.1:${port}/\\1`,
      },
    },
    condition: {
      regexFilter: `^https://${host.replace('.', '\\.')}/(.+)$`,
      resourceTypes: [
        chrome.declarativeNetRequest.ResourceType.XMLHTTPREQUEST,
      ],
    },
  }));

  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      addRules: rules,
    });
    console.log(`[ToolRecall] DNR rules installed for ${API_HOSTS.length} API hosts → 127.0.0.1:${port}`);
  } catch (err) {
    console.warn('[ToolRecall] Failed to install DNR rules:', err);
  }
}

/**
 * Remove all ToolRecall DNR rules.
 */
export async function removeDnrRules(): Promise<void> {
  if (!chrome.declarativeNetRequest) return;

  const ids: number[] = API_HOSTS.map((_, i) => ruleId(i));
  try {
    await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: ids });
    console.log('[ToolRecall] DNR rules removed');
  } catch {
    // ignore
  }
}
