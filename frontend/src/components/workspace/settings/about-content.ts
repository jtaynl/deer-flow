/**
 * About WRI AI markdown content. Inlined to avoid raw-loader dependency
 * (Turbopack cannot resolve raw-loader for .md imports).
 */
export const aboutMarkdown = `# About WRI AI

> **Empowering Decisions Through World Intelligence**

**WRI AI** is the AI-powered intelligence assistant of the [World Research Institute](https://www.worldresearch.org). It brings WRI's analyst methodology to a conversational interface — making analyst-quality research available on demand, with every claim backed by a verified-source citation chain.

---

## What WRI AI Does

WRI AI mirrors WRI's four core research services:

* **Market Research** — sizing, dynamics, customer behaviour, and entry feasibility across global sectors
* **Industry Analysis** — structure, competitive landscape, regulatory environment, and growth trajectories
* **Trend Forecasting** — emerging trends, technology shifts, and macroeconomic forces shaping tomorrow's environment
* **Competitive Intelligence** — competitor strategies, capabilities, positioning, and market signals

Each capability is grounded in the same methodology WRI's human analysts use.

---

## How It Works

1. **Ask** — frame your research question in plain language
2. **Synthesise** — WRI AI pulls from primary and secondary sources, runs structured analysis, and verifies every cited claim against the original page
3. **Refine** — iterate inline, drill into any claim, export the synthesis

Every output carries a provenance trail back to its sources.

---

## Why You Can Trust It

* **Data Accuracy** — three-layer source verification (URL liveness → content extraction → claim-match LLM judging) on every citation
* **Expert Analysts** — calibrated against the methodology of WRI's global sector specialists
* **Global Coverage** — 150+ country coverage with localised sources and region-aware aggregation
* **Actionable Insights** — outputs structured for decision-making, with confidence flags and contribution analysis

---

## Built On

WRI AI runs on the open-source **DeerFlow** super-agent framework from ByteDance, plus deterministic Python pipelines for the domains where reproducibility matters (forecasting, aggregation, citation verification). We're grateful to the open-source community whose work makes this possible:

* **[DeerFlow](https://github.com/bytedance/deer-flow)** — the agent harness orchestrating sub-agents, memory, and sandboxes
* **[LangChain](https://github.com/langchain-ai/langchain)** & **[LangGraph](https://github.com/langchain-ai/langgraph)** — LLM interaction and multi-agent orchestration
* **[Next.js](https://nextjs.org/)** — the web application framework
* **[Shadcn UI](https://ui.shadcn.com/)** — UI component primitives

---

## Connect with WRI

* 🌐 **Main site**: [worldresearch.org](https://www.worldresearch.org)
* 💼 **Services**: [worldresearch.org/services](https://www.worldresearch.org/services/)
* 📊 **Insights**: [worldresearch.org/insights](https://www.worldresearch.org/insights/)
* ✉️ **Contact**: [worldresearch.org/contact](https://www.worldresearch.org/contact/)

---

## License

WRI AI's underlying agent framework (DeerFlow) is distributed under the **MIT License**. Methodology, weightings, and content produced by this deployment are proprietary to the World Research Institute.
`;
