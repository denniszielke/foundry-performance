import { HttpAgent } from "@ag-ui/client";
import type { BaseEvent, Message } from "@ag-ui/core";
import {
  AlertTriangle,
  Check,
  ChevronRight,
  CircleDot,
  FileSearch,
  FlaskConical,
  Globe2,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { FormEvent, useRef, useState } from "react";

type Workflow = {
  workflow_id: string;
  status: string;
  hypothesis?: {
    statement: string;
    assumptions: string[];
    evidence_needed: string[];
    success_criteria: string[];
    risks_and_constraints: string[];
  };
  plan?: {
    objective: string;
    steps: Array<{
      id: number;
      title: string;
      description: string;
      tool_category: "internet_research" | "context_api" | "document_search";
      expected_evidence: string[];
      completion_criteria: string[];
    }>;
  };
  plan_revision: number;
  plan_digest: string;
  tool_activity: Array<{
    phase: string;
    tool_name: string;
    duration_ms: number;
    citations: string[];
  }>;
  execution?: { final_answer: string; warnings: string[]; artifacts: Array<Record<string, unknown>> };
  error?: string;
};

const toolMeta = {
  internet_research: { label: "Internet research", icon: Globe2 },
  context_api: { label: "Context API", icon: Search },
  document_search: { label: "Document search", icon: FileSearch },
};

const makeId = () => crypto.randomUUID();

export function App() {
  const [scenario, setScenario] = useState("");
  const [comment, setComment] = useState("");
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const threadId = useRef(makeId());
  const agent = useRef(new HttpAgent({ url: "/api/agent", threadId: threadId.current }));

  async function invoke(control: Record<string, unknown>) {
    setBusy(true);
    setError("");
    agent.current.setState({ workflow: control });
    const input = {
      threadId: threadId.current,
      runId: makeId(),
      messages: [] as Message[],
      state: { workflow: control },
      tools: [],
      context: [],
      forwardedProps: {},
    };
    await new Promise<void>((resolve, reject) => {
      agent.current.run(input).subscribe({
        next: (event: BaseEvent) => {
          if (event.type === "STATE_SNAPSHOT") {
            const snapshot = (event as unknown as { snapshot: { workflow: Workflow } }).snapshot;
            setWorkflow(snapshot.workflow);
          }
        },
        error: reject,
        complete: resolve,
      });
    }).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : String(reason));
    });
    setBusy(false);
  }

  function submitScenario(event: FormEvent) {
    event.preventDefault();
    if (!scenario.trim()) return;
    void invoke({
      action: "plan",
      scenario: scenario.trim(),
      client_request_id: makeId(),
    });
  }

  function decide(decision: "approved" | "rejected" | "revise") {
    if (!workflow) return;
    void invoke({
      action: "approve",
      workflow_id: workflow.workflow_id,
      plan_revision: workflow.plan_revision,
      plan_digest: workflow.plan_digest,
      decision,
      comment: comment.trim() || undefined,
      client_request_id: makeId(),
    });
  }

  function reset() {
    agent.current.abortRun();
    threadId.current = makeId();
    agent.current = new HttpAgent({ url: "/api/agent", threadId: threadId.current });
    setWorkflow(null);
    setScenario("");
    setComment("");
    setError("");
  }

  const waiting = workflow?.status === "awaiting_approval";
  const finished = ["completed", "rejected", "failed"].includes(workflow?.status ?? "");

  return (
    <main>
      <header className="topbar">
        <div className="brand"><FlaskConical size={19} /><span>Hypothesis Workbench</span></div>
        <div className="status-cluster">
          <span className={`status-dot ${busy ? "working" : ""}`} />
          <span>{busy ? "Agent working" : "Ready"}</span>
          <button className="icon-button" onClick={reset} title="New workflow" aria-label="New workflow"><RotateCcw size={17} /></button>
        </div>
      </header>

      <section className="workspace">
        <aside className="rail">
          <p className="eyebrow">Workflow</p>
          {["Hypothesis", "Research plan", "Approval", "Execution"].map((label, index) => {
            const active = workflow ? (finished ? 3 : waiting ? 2 : 1) : 0;
            return <div className={`rail-step ${index <= active ? "active" : ""}`} key={label}><span>{index + 1}</span>{label}</div>;
          })}
          {workflow && <div className="workflow-id"><span>Workflow ID</span><code>{workflow.workflow_id.slice(0, 12)}</code><span>Revision {workflow.plan_revision}</span></div>}
        </aside>

        <div className="content">
          {!workflow && !busy && (
            <section className="scenario-panel">
              <div className="scenario-heading"><CircleDot size={20} /><div><h1>Frame a testable scenario</h1><p>The agent will formulate a hypothesis and prepare a bounded evidence plan for review.</p></div></div>
              <form onSubmit={submitScenario}>
                <textarea value={scenario} onChange={(event) => setScenario(event.target.value)} placeholder="Example: Test whether Seattle is currently warmer than Sydney, using current weather evidence." autoFocus />
                <div className="form-footer"><span>{scenario.length.toLocaleString()} / 50,000</span><button className="primary" disabled={!scenario.trim()}><FlaskConical size={17} />Build hypothesis</button></div>
              </form>
            </section>
          )}

          {busy && !workflow && <LoadingState label="Formulating the hypothesis and research plan" />}

          {workflow && (
            <>
              <section className="title-row"><div><p className="eyebrow">Current hypothesis</p><h1>{workflow.hypothesis?.statement ?? "Hypothesis workflow"}</h1></div><span className={`status-pill ${workflow.status}`}>{workflow.status.replaceAll("_", " ")}</span></section>

              <section className="evidence-grid">
                <InfoList title="Assumptions" items={workflow.hypothesis?.assumptions} />
                <InfoList title="Evidence needed" items={workflow.hypothesis?.evidence_needed} />
                <InfoList title="Success criteria" items={workflow.hypothesis?.success_criteria} />
                <InfoList title="Risks & constraints" items={workflow.hypothesis?.risks_and_constraints} warning />
              </section>

              {workflow.plan && <section className="plan-section"><div className="section-heading"><div><p className="eyebrow">Proposed research plan</p><h2>{workflow.plan.objective}</h2></div><span>{workflow.plan.steps.length} steps</span></div><div className="steps">{workflow.plan.steps.map((step) => { const meta = toolMeta[step.tool_category]; const Icon = meta.icon; return <article className="step" key={step.id}><div className="step-index">{step.id}</div><div className="step-body"><div className="step-title"><h3>{step.title}</h3><span><Icon size={14} />{meta.label}</span></div><p>{step.description}</p><div className="step-details"><div><b>Expected evidence</b>{step.expected_evidence.map((item) => <span key={item}><ChevronRight size={13} />{item}</span>)}</div><div><b>Complete when</b>{step.completion_criteria.map((item) => <span key={item}><Check size={13} />{item}</span>)}</div></div></div></article>; })}</div></section>}

              {waiting && <section className="approval"><div className="approval-heading"><ShieldCheck size={21} /><div><h2>Human approval required</h2><p>Execution is blocked until this exact plan revision and digest are approved.</p></div></div><textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Optional feedback for revision or rejection" /><div className="approval-actions"><button className="secondary danger" disabled={busy} onClick={() => decide("rejected")}><X size={17} />Reject</button><button className="secondary" disabled={busy} onClick={() => decide("revise")}><RefreshCw size={17} />Request revision</button><button className="primary" disabled={busy} onClick={() => decide("approved")}><Check size={17} />Approve & execute</button></div><code className="digest">{workflow.plan_digest}</code></section>}

              {busy && <LoadingState label={waiting ? "Applying your decision" : "Running workflow"} compact />}
              {workflow.execution && <section className="result"><p className="eyebrow">Execution result</p><h2>Findings</h2><div className="answer">{workflow.execution.final_answer}</div>{workflow.execution.warnings.length > 0 && <InfoList title="Warnings" items={workflow.execution.warnings} warning />}</section>}
              {workflow.error && <div className="error-banner"><AlertTriangle size={18} />{workflow.error}</div>}
            </>
          )}
          {error && <div className="error-banner"><AlertTriangle size={18} />{error}</div>}
        </div>
      </section>
    </main>
  );
}

function InfoList({ title, items = [], warning = false }: { title: string; items?: string[]; warning?: boolean }) {
  return <article className={`info-block ${warning ? "warning" : ""}`}><h3>{warning && <AlertTriangle size={15} />}{title}</h3>{items.length ? <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul> : <p>None specified</p>}</article>;
}

function LoadingState({ label, compact = false }: { label: string; compact?: boolean }) {
  return <div className={`loading ${compact ? "compact" : ""}`}><LoaderCircle className="spin" size={22} /><span>{label}</span></div>;
}