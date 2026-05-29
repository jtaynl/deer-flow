import { Footer } from "@/components/landing/footer";
import { Header } from "@/components/landing/header";
import { Hero } from "@/components/landing/hero";
import { CapabilitiesSection } from "@/components/landing/sections/capabilities-section";
import { CTASection } from "@/components/landing/sections/cta-section";
import { HowItWorksSection } from "@/components/landing/sections/how-it-works-section";
import { TrustSection } from "@/components/landing/sections/trust-section";

export default function LandingPage() {
  return (
    <div className="min-h-screen w-full bg-white text-[#0a1628]">
      <Header />
      <main className="flex w-full flex-col">
        <Hero />
        <CapabilitiesSection />
        <HowItWorksSection />
        <TrustSection />
        <CTASection />
      </main>
      <Footer />
    </div>
  );
}
