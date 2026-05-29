import {
  BarChart3Icon,
  GlobeIcon,
  LineChartIcon,
  TargetIcon,
} from "lucide-react";

const CAPABILITIES = [
  {
    icon: BarChart3Icon,
    title: "Market Research",
    description:
      "Sizing, dynamics, customer behaviour, and entry feasibility across global sectors. Primary and secondary sources synthesised in minutes, not weeks.",
  },
  {
    icon: GlobeIcon,
    title: "Industry Analysis",
    description:
      "Structure, competitive landscape, regulatory environment, and growth trajectories — with the analytical depth WRI's reports are known for.",
  },
  {
    icon: LineChartIcon,
    title: "Trend Forecasting",
    description:
      "Forward-looking analysis of emerging market trends, technology shifts, and macroeconomic forces. Backed by methodology and reproducible computation.",
  },
  {
    icon: TargetIcon,
    title: "Competitive Intelligence",
    description:
      "Track competitor strategies, capabilities, positioning, and signals across markets. Cited evidence on every claim, surfaced to your workspace.",
  },
];

export function CapabilitiesSection() {
  return (
    <section
      id="capabilities"
      className="relative w-full bg-white py-20 md:py-28"
    >
      <div className="container-md mx-auto flex flex-col items-center px-6">
        <div className="text-center">
          <div className="text-xs font-semibold tracking-[0.2em] text-[#7b1e2b] uppercase">
            What WRI AI Does
          </div>
          <h2 className="mt-3 max-w-3xl text-balance text-3xl font-semibold tracking-tight text-[#0a1628] md:text-4xl">
            Research that mirrors WRI&rsquo;s four core services.
          </h2>
          <p className="mt-4 max-w-2xl text-balance text-base leading-relaxed text-[#4b5563] md:text-lg">
            Each capability is grounded in the same methodology WRI&rsquo;s
            human analysts use &mdash; with the speed and breadth of an AI
            assistant on top.
          </p>
        </div>

        <div className="mt-16 grid w-full max-w-5xl grid-cols-1 gap-6 md:grid-cols-2">
          {CAPABILITIES.map((cap) => (
            <div
              key={cap.title}
              className="group relative flex flex-col rounded-2xl border border-[#e5e7eb] bg-white p-8 transition-all hover:border-[#7b1e2b]/40 hover:shadow-lg hover:shadow-[#7b1e2b]/5"
            >
              <div className="inline-flex size-12 items-center justify-center rounded-xl bg-[#fdf2f3] text-[#7b1e2b] transition-colors group-hover:bg-[#7b1e2b] group-hover:text-white">
                <cap.icon className="size-6" strokeWidth={1.75} />
              </div>
              <h3 className="mt-6 text-xl font-semibold tracking-tight text-[#0a1628]">
                {cap.title}
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-[#4b5563]">
                {cap.description}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
