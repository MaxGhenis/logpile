import type { Turn } from "@/lib/parsers";
import { fmtTs } from "@/lib/format";

function TurnUser({ turn }: { turn: Extract<Turn, { type: "user" }> }) {
  return (
    <div className="rounded-lg px-4 py-3 border-l-[3px] border-l-lp-amber bg-lp-amber/[0.04]">
      <div className="text-[0.68rem] font-bold uppercase tracking-wider text-lp-amber mb-2 flex items-center gap-2.5">
        User
        {turn.cwd && (
          <span className="font-normal text-lp-text-faint font-mono text-[0.7rem]">
            {turn.cwd}
          </span>
        )}
      </div>
      <div className="text-sm leading-relaxed whitespace-pre-wrap break-words">
        {turn.content}
      </div>
      {turn.timestamp && (
        <div className="text-[0.7rem] text-lp-text-faint mt-2 text-right font-mono">
          {fmtTs(turn.timestamp)}
        </div>
      )}
    </div>
  );
}

function TurnAssistant({ turn }: { turn: Extract<Turn, { type: "assistant" }> }) {
  return (
    <div className="rounded-lg px-4 py-3 border-l-[3px] border-l-lp-green bg-lp-green/[0.04]">
      <div className="text-[0.68rem] font-bold uppercase tracking-wider text-lp-green mb-2 flex items-center gap-2.5">
        Assistant
        {turn.model && (
          <span className="font-medium text-lp-text-faint font-mono text-[0.7rem]">
            {turn.model}
          </span>
        )}
        {turn.output_tokens > 0 && (
          <span className="font-medium text-lp-text-faint font-mono text-[0.7rem]">
            {turn.input_tokens}&rarr;{turn.output_tokens} tok
          </span>
        )}
      </div>
      <div className="text-sm leading-relaxed">
        {turn.blocks.map((block, i) => {
          if (block.type === "text") {
            return (
              <div key={i} className="whitespace-pre-wrap break-words mb-2">
                {block.text}
              </div>
            );
          }
          if (block.type === "thinking") {
            return (
              <details
                key={i}
                className="border border-lp-border-dim rounded my-2"
              >
                <summary className="px-3 py-1.5 cursor-pointer text-lp-text-faint text-xs italic select-none">
                  Thinking...
                </summary>
                <div className="px-3 py-2.5 text-lp-text-faint text-xs whitespace-pre-wrap border-t border-lp-border-dim leading-relaxed">
                  {block.text}
                </div>
              </details>
            );
          }
          if (block.type === "tool_use") {
            const cmd = String(
              block.input?.command ??
              block.input?.file_path ??
              block.input?.pattern ??
              ""
            ) || null;
            return (
              <details
                key={i}
                open
                className="border border-lp-blue/15 rounded my-1.5 overflow-hidden"
              >
                <summary className="px-3 py-1.5 cursor-pointer text-lp-blue text-xs select-none flex items-center gap-2">
                  <span className="font-bold font-mono">{block.name}</span>
                  {cmd && (
                    <span className="text-lp-text-faint font-mono flex-1 truncate text-[0.78rem]">
                      {String(cmd).slice(0, 60)}
                    </span>
                  )}
                </summary>
                <pre className="m-0 p-3 bg-lp-bg border-t border-lp-border-dim text-[0.78rem] font-mono overflow-x-auto text-lp-text-faint max-h-[300px] overflow-y-auto">
                  {JSON.stringify(block.input, null, 2)}
                </pre>
              </details>
            );
          }
          return null;
        })}
      </div>
      {turn.timestamp && (
        <div className="text-[0.7rem] text-lp-text-faint mt-2 text-right font-mono">
          {fmtTs(turn.timestamp)}
        </div>
      )}
    </div>
  );
}

function TurnToolUse({ turn }: { turn: Extract<Turn, { type: "tool_use" }> }) {
  const cmd = turn.input?.command ? String(turn.input.command) : null;
  return (
    <div className="rounded-lg px-4 py-3 border-l-[3px] border-l-lp-blue bg-lp-blue/[0.04]">
      <div className="text-[0.68rem] font-bold uppercase tracking-wider text-lp-text-faint mb-2">
        Tool call
      </div>
      <details open className="border border-lp-blue/15 rounded overflow-hidden">
        <summary className="px-3 py-1.5 cursor-pointer text-lp-blue text-xs select-none flex items-center gap-2">
          <span className="font-bold font-mono">{turn.name}</span>
          {cmd && (
            <span className="text-lp-text-faint font-mono flex-1 truncate text-[0.78rem]">
              {String(cmd).slice(0, 60)}
            </span>
          )}
        </summary>
        <pre className="m-0 p-3 bg-lp-bg border-t border-lp-border-dim text-[0.78rem] font-mono overflow-x-auto text-lp-text-faint max-h-[300px] overflow-y-auto">
          {JSON.stringify(turn.input, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function TurnToolResult({
  turn,
}: {
  turn: Extract<Turn, { type: "tool_result" }>;
}) {
  return (
    <div
      className={`rounded-lg px-4 py-3 border-l-[3px] ${
        turn.is_error
          ? "border-l-lp-red bg-lp-red/[0.04]"
          : "border-l-lp-border bg-lp-surface"
      }`}
    >
      <div
        className={`text-[0.68rem] font-bold uppercase tracking-wider mb-2 ${
          turn.is_error ? "text-lp-red" : "text-lp-text-faint"
        }`}
      >
        {turn.is_error ? "Error" : "Result"}
      </div>
      <details>
        <summary className="cursor-pointer text-lp-text-faint text-xs font-mono select-none truncate">
          {turn.content.slice(0, 100)}
        </summary>
        <pre className="mt-2 p-2.5 bg-lp-bg rounded text-[0.76rem] font-mono overflow-x-auto text-lp-text-faint max-h-[400px] overflow-y-auto whitespace-pre">
          {turn.content}
        </pre>
      </details>
    </div>
  );
}

function TurnThinking({ turn }: { turn: Extract<Turn, { type: "thinking" }> }) {
  return (
    <div className="rounded-lg px-4 py-3 border-l-[3px] border-l-lp-border-dim bg-lp-surface">
      <details className="border border-lp-border-dim rounded">
        <summary className="px-3 py-1.5 cursor-pointer text-lp-text-faint text-xs italic select-none">
          Reasoning...
        </summary>
        <div className="px-3 py-2.5 text-lp-text-faint text-xs whitespace-pre-wrap border-t border-lp-border-dim leading-relaxed">
          {turn.text}
        </div>
      </details>
    </div>
  );
}

export function Transcript({ turns }: { turns: Turn[] }) {
  if (turns.length === 0) {
    return (
      <div className="text-center text-lp-text-faint py-10 italic">
        No transcript available.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-0.5">
      {turns.map((turn, i) => {
        switch (turn.type) {
          case "user":
            return <TurnUser key={i} turn={turn} />;
          case "assistant":
            return <TurnAssistant key={i} turn={turn} />;
          case "tool_use":
            return <TurnToolUse key={i} turn={turn} />;
          case "tool_result":
            return <TurnToolResult key={i} turn={turn} />;
          case "thinking":
            return <TurnThinking key={i} turn={turn} />;
          case "error":
            return (
              <div
                key={i}
                className="rounded-lg px-4 py-3 border-l-[3px] border-l-lp-red bg-lp-red/[0.04] text-sm"
              >
                {turn.content}
              </div>
            );
          default:
            return null;
        }
      })}
    </div>
  );
}
