import { CheckCircle2Icon } from "lucide-react";

const STEPS = [
  {
    number: "01",
    title: "Ask",
    description:
      "Frame your research question in plain language. Scope it tightly or broadly — the assistant clarifies before it starts.",
    examples: [
      "What's driving Singapore's logistics sector in Q3?",
      "Compare EV charger market entry strategies in Brazil vs Mexico.",
      "Forecast Asia-Pacific freight cost pressure for next quarter.",
    ],
  },
  {
    number: "02",
    title: "Synthesise",
    description:
      "WRI AI pulls from primary and secondary sources, computes structured analysis, and verifies every cited claim against the original page.",
    examples: [
      "Multi-source synthesis with citation chains",
      "Deterministic computation for tables and projections",
      "Three-layer source verification (live → content → claim match)",
    ],
  },
  {
    number: "03",
    title: "Refine",
    description:
      "Drill into any claim, request comparisons, or export the synthesis. Every analysis carries a provenance trail back to its sources.",
    examples: [
      "Iterate inline — ask follow-up questions in the same thread",
      "Export to markdown, structured tables, or workbook overlays",
      "Source audit log per claim for compliance/review",
    ],
  },
];

export function HowItWorksSection() {
  return (
    <section
      id="how-it-works"
      className="relative w-full bg-[#faf7f2] py-20 md:py-28"
    >
      <div className="container-md mx-auto flex flex-col items-center px-6">
        <div className="text-center">
          <div className="text-xs font-semibold tracking-[0.2em] text-[#7b1e2b] uppercase">
            How It Works
          </div>
          <h2 className="mt-3 max-w-3xl text-balance text-3xl font-semibold tracking-tight text-[#0a1628] md:text-4xl">
            Conversational interface. Analyst-grade output.
          </h2>
          <p className="mt-4 max-w-2xl text-balance text-base leading-relaxed text-[#4b5563] md:text-lg">
            Three steps from research question to defensible insight.
          </p>
        </div>

        <div className="mt-16 grid w-full max-w-5xl grid-cols-1 gap-8 md:grid-cols-3">
          {STEPS.map((step) => (
            <div
              key={step.number}
              className="relative flex flex-col rounded-2xl border border-[#e5e7eb] bg-white p-8 shadow-sm"
            >
              <div className="font-mono text-sm font-semibold tracking-wider text-[#7b1e2b]">
                {step.number}
              </div>
              <h3 className="mt-2 text-2xl font-semibold tracking-tight text-[#0a1628]">
                {step.title}
              </h3>
              <p className="mt-3 text-sm leading-relaxed text-[#4b5563]">
                {step.description}
              </p>
              <ul className="mt-6 space-y-2.5">
                {step.examples.map((ex) => (
                  <li
                    key={ex}
                    className="flex items-start gap-2 text-sm text-[#374151]"
                  >
                    <CheckCircle2Icon
                      className="mt-0.5 size-4 flex-shrink-0 text-[#7b1e2b]"
                      strokeWidth={2}
                    />
                    <span>{ex}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
