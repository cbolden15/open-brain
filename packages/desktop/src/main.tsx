import { invoke } from "@tauri-apps/api/core";
import { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import { proofSummary, type NativeProofResult, type ProofState } from "./contracts";
import "./styles.css";

function App() {
  const [state, setState] = useState<ProofState>({ kind: "running" });

  const runProof = useCallback(async () => {
    setState({ kind: "running" });
    try {
      const result = await invoke<NativeProofResult>("run_native_proof");
      setState({ kind: "passed", result });
    } catch (error) {
      setState({ kind: "failed", code: String(error) });
    }
  }, []);

  useEffect(() => {
    void runProof();
  }, [runProof]);

  return (
    <main>
      <p className="eyebrow">Milestone D0 · native boundary</p>
      <h1>Open Brain Desktop</h1>
      <p className={`status status-${state.kind}`} aria-live="polite">
        {proofSummary(state)}
      </p>
      {state.kind === "passed" ? (
        <dl>
          <div>
            <dt>Runtime</dt>
            <dd>{state.result.productVersion}</dd>
          </div>
          <div>
            <dt>Protocol</dt>
            <dd>{`${state.result.protocol} v${state.result.protocolVersion}`}</dd>
          </div>
          <div>
            <dt>Capture</dt>
            <dd>{state.result.captureStatus}</dd>
          </div>
          <div>
            <dt>Search matches</dt>
            <dd>{state.result.matchedSearchResults}</dd>
          </div>
          <div>
            <dt>Graphify</dt>
            <dd>{state.result.graphifyStatus}</dd>
          </div>
          <div>
            <dt>Process group</dt>
            <dd>{state.result.cleanupConfirmed ? "terminated" : "unconfirmed"}</dd>
          </div>
          <div>
            <dt>Target</dt>
            <dd>{state.result.target}</dd>
          </div>
        </dl>
      ) : null}
      <button type="button" disabled={state.kind === "running"} onClick={() => void runProof()}>
        {state.kind === "running" ? "Running proof…" : "Run proof again"}
      </button>
      <p className="boundary">
        Uses only the matched runtime inside this app bundle. It does not replace or launch an
        installed Open Brain CLI.
      </p>
    </main>
  );
}

const root = document.getElementById("root");
if (root === null) throw new Error("root_missing");
createRoot(root).render(<App />);
