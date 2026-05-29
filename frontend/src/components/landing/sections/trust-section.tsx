import {
  GlobeIcon,
  ShieldCheckIcon,
  SparklesIcon,
  UsersIcon,
} from "lucide-react";

const PILLARS = [
  {
    icon: ShieldCheckIcon,
    title: "Data Accuracy",
    description:
      "Every cited claim is verified against the live source page via a three-layer audit. Bad URLs and off-topic citations are flagged before they reach you.",
    proof: "Stage 2 verification: HEAD → content → claim-match",
  },
  {
    icon: UsersIcon,
    title: "Expert Analysts",
    description:
      "WRI AI is calibrated against the methodology of WRI's global sector specialists. The framework, weightings, and rigour come from 20+ years of human practice.",
    proof: "Methodology and weightings reviewed by WRI analysts",
  },
  {
    icon: GlobeIcon,
    title: "Global Coverage",
    description:
      "150+ country coverage with localised sources. The assistant respects regional context — language, market structure, regulatory regime — when synthesising.",
    proof: "Per-country source preference + region-aware aggregation",
  },
  {
    icon: SparklesIcon,
    title: "Actionable Insights",
    description:
      "Output is structured for decision-making: ranked findings, contribution analysis, confidence flags. No raw data dump — every insight is decision-ready.",
    proof: "Outputs include confidence, contribution, and audit trail",
  },
];

export function TrustSection() {
  return (
    <section
      id="trust"
      className="relative w-full bg-white py-20 md:py-28"
    >
      <div className="container-md mx-auto flex flex-col items-center px-6">
        <div className="text-center">
          <div className="text-xs font-semibold tracking-[0.2em] text-[#7b1e2b] uppercase">
            Why WRI AI
          </div>
          <h2 className="mt-3 max-w-3xl text-balance text-3xl font-semibold tracking-tight text-[#0a1628] md:text-4xl">
            Intelligence you can trust and act on.
          </h2>
          <p className="mt-4 max-w-2xl text-balance text-base leading-relaxed text-[#4b5563] md:text-lg">
            The same four pillars that anchor WRI&rsquo;s human research practice,
            engineered into the assistant.
          </p>
        </div>

        <div className="mt-16 grid w-full max-w-5xl grid-cols-1 gap-x-12 gap-y-12 md:grid-cols-2">
          {PILLARS.map((pillar) => (
            <div key={pillar.title} className="flex gap-5">
              <div className="flex-shrink-0">
                <div className="inline-flex size-12 items-center justify-center rounded-xl bg-[#0a1628] text-white">
                  <pillar.icon className="size-6" strokeWidth={1.75} />
                </div>
              </div>
              <div className="flex-1">
                <h3 className="text-lg font-semibold tracking-tight text-[#0a1628]">
                  {pillar.title}
                </h3>
                <p className="mt-2 text-sm leading-relaxed text-[#4b5563]">
                  {pillar.description}
                </p>
                <div className="mt-3 inline-flex items-center gap-1.5 rounded-full bg-[#fdf2f3] px-3 py-1 text-xs font-medium text-[#7b1e2b]">
                  <span className="size-1.5 rounded-full bg-[#7b1e2b]" />
                  {pillar.proof}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
