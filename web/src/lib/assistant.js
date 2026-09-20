//: The assistant loop, run in the browser so the key never leaves it.
//:
//: Labtris is meant to be installed by people who are not the only person
//: using it. If the server held the key, every operator would be holding a
//: credential belonging to someone else, and the honest way to promise that
//: cannot leak is to never receive it. So the browser talks to the model
//: directly, and comes back to Labtris only to run what the model decided to
//: call — against that user's own session, so the assistant can do exactly
//: what they could and nothing more.

import { api } from "./api.js";

const KEY = "labtris.llm";

export function loadConfig() {
  const fallback = { baseUrl: "https://api.openai.com/v1", model: "", apiKey: "" };
  try {
    return { ...fallback, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
  } catch {
    return fallback;
  }
}

export function saveConfig(patch) {
  const next = { ...loadConfig(), ...patch };
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    //: Storage disabled: the setting still applies for this session. Losing it
    //: on reload beats refusing to accept it.
  }
  return next;
}

export function forgetKey() {
  const c = loadConfig();
  saveConfig({ ...c, apiKey: "" });
}

async function chat(cfg, messages, tools, signal) {
  const doPost = (msgs) =>
    fetch(cfg.baseUrl.replace(/\/+$/, "") + "/chat/completions", {
      method: "POST",
      signal,
      headers: {
        "content-type": "application/json",
        ...(cfg.apiKey ? { authorization: `Bearer ${cfg.apiKey}` } : {}),
      },
      body: JSON.stringify({
        model: cfg.model,
        messages: msgs,
        tools,
        tool_choice: "auto",
      }),
    });

  let res = await doPost(messages);
  //: One class of provider 400 is worth retrying inline: the model produced
  //: a tool-call payload with malformed JSON (unclosed quotes in `command`,
  //: unescaped newlines in a config blob, ...). The gateway rejects the
  //: whole response so we cannot even feed it back as a tool_result the
  //: model could self-correct from. One retry with a nudge appended to the
  //: user turn usually gets clean JSON second time; if not, the error we
  //: throw below at least names what went wrong.
  if (!res.ok && res.status === 400) {
    const body = await res.clone().text().catch(() => "");
    if (/Failed to parse tool call arguments|Unexpected token|JSONDecodeError|malformed/i.test(body)) {
      const nudged = [
        ...messages,
        {
          role: "user",
          content:
            "(Your previous tool call could not be parsed. Reissue it with " +
            "valid JSON — escape newlines as \\n, quote strings with double " +
            "quotes, and keep the payload compact.)",
        },
      ];
      res = await doPost(nudged);
    }
  }

  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    //: The provider's own words. "401" alone sends people to check Labtris,
    //: which is the one place the problem cannot be. On 400 with a
    //: tool-call parse error we add one line naming the actual cause,
    //: because litellm's error envelope buries it three levels deep.
    let hint = "";
    if (
      res.status === 400 &&
      /Failed to parse tool call arguments|Expecting.*delimiter/i.test(detail)
    ) {
      hint =
        "\n\nThe model produced malformed JSON in a tool call and the retry " +
        "did not clean it up. Rephrase your request or try again — the " +
        "provider drops the whole turn when this happens, so nothing was " +
        "executed. Common cause: a very long `command` payload with " +
        "special characters.";
    }
    throw new Error(`${cfg.baseUrl} returned ${res.status}: ${detail.slice(0, 300)}${hint}`);
  }
  return res.json();
}

/**
 * Run one turn to completion. Emits events during the loop so the UI can
 * show each step as it happens rather than a wall of history at the end:
 *
 *   onStep({name, args})           — a tool is about to run
 *   onTurn({text, applied})        — one model turn finished (text +
 *                                    the tool calls it made this turn)
 *   onError({name, args, error})   — a tool refused; the model will get
 *                                    the error back and can retry/adapt
 *                                    (mirrored to the applied list too)
 *
 * The final return still carries the whole transcript so existing callers
 * that don't wire up onTurn keep working.
 */
export async function runTurn({
  labId,
  message,
  history = [],
  onStep,
  onTurn,
  onError,
  signal,
  //: Runaway backstop, not a normal limit. The loop terminates naturally
  //: when the model returns without tool calls — matches Claude Code and
  //: the server-side path (labtris_api/config.py llm_max_steps). Only
  //: kicks in if a model gets stuck calling tools forever; the Stop
  //: button in the UI is the primary user-controlled interruption.
  maxSteps = 500,
  //: Injectable so the loop can be tested without a provider and without a
  //: server. The logic worth testing is what happens between them — whether a
  //: refusal is fed back, whether the step cap is honoured, whether a turn
  //: with no tool calls ends — and none of that needs a real model.
  deps = {},
}) {
  const cfg = deps.config ?? loadConfig();
  if (!cfg.apiKey) throw new Error("No API key set. Add one in Settings → AI assistant.");
  if (!cfg.model) throw new Error("No model set. Add one in Settings → AI assistant.");

  const callProvider = deps.chat ?? chat;
  const execTool = deps.execTool ?? ((body) => api.agentTool(body));
  const fetchTools = deps.fetchTools ?? (() => api.agentTools());

  const { tools, system } = await fetchTools();
  const messages = [
    { role: "system", content: system },
    ...history,
    { role: "user", content: `Lab id: ${labId}\n\n${message}` },
  ];
  const applied = [];

  for (let step = 0; step < maxSteps; step++) {
    const data = await callProvider(cfg, messages, tools, signal);
    const choice = data.choices?.[0]?.message;
    if (!choice) throw new Error("The model returned no message.");
    messages.push(choice);

    const calls = choice.tool_calls || [];
    if (!calls.length) {
      const finalText = choice.content || "";
      onTurn?.({ text: finalText, applied: [] });
      return { text: finalText, applied, messages };
    }

    //: Emit the model's reasoning text (if any) as a mid-turn bubble
    //: BEFORE the tool calls run. Otherwise a model that says "I'll
    //: check the health first, then add the nodes" and then calls
    //: health/add_node in one turn shows nothing until every call
    //: finishes — the user has no idea what the plan is.
    const turnText = choice.content || "";
    const turnApplied = [];

    for (const call of calls) {
      let args = {};
      try {
        args = JSON.parse(call.function.arguments || "{}");
      } catch {
        args = {};
      }
      onStep?.({ name: call.function.name, args });
      //: Executed on the server as this user. A refusal comes back as content
      //: rather than an exception, because it is information the model should
      //: act on — "that lab is locked" is an answer, not a crash.
      const out = await execTool({
        name: call.function.name,
        arguments: args,
        lab_id: labId,
      });
      const argsStr = Object.entries(args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
      if (out.ok) {
        const line = `${call.function.name}(${argsStr})`;
        applied.push(line);
        turnApplied.push(line);
      } else {
        //: A refused tool call is the single most useful thing to
        //: surface. The model got the error back and may retry or
        //: adapt, but the user needs to see what went wrong — the
        //: previous shape hid it in a small "applied" list below the
        //: final bubble, easy to miss.
        const line = `${call.function.name}(${argsStr}) — ${out.error}`;
        applied.push(`${call.function.name} refused: ${out.error}`);
        turnApplied.push(line);
        onError?.({ name: call.function.name, args, error: out.error });
      }
      messages.push({
        role: "tool",
        tool_call_id: call.id,
        content: JSON.stringify(out.ok ? out.result : { error: out.error }),
      });
    }
    //: Emit the assembled turn so the UI can render it as one bubble
    //: (matching the server-side WebSocket path's shape). onTurn is
    //: fired AFTER the tool calls resolve so the bubble carries their
    //: outcomes; onError already fired inline for the failures.
    onTurn?.({ text: turnText, applied: turnApplied });
  }
  //: Hit the runaway backstop. Match the server-side wording so users
  //: see the same story regardless of which path is active.
  const summary = applied.length
    ? applied.map((a) => `  • ${a}`).join("\n")
    : "  (nothing landed — the model was stuck in a reasoning loop)";
  return {
    text:
      `Hit the runaway backstop at ${maxSteps} tool calls in one turn.\n` +
      `Here's what did land (${applied.length} call${applied.length === 1 ? "" : "s"}):\n` +
      `${summary}\n\n` +
      `To keep going: say "continue".`,
    applied,
    messages,
    truncated: true,
  };
}
