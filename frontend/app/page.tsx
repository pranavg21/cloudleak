import Link from "next/link";
import { CsvUploader } from "@/components/csv-uploader";

const STEPS = [
  {
    title: "Export your bill",
    body: "Azure Cost Management, an AWS Cost and Usage Report, or a GCP billing export. The raw .csv, exactly as it downloads.",
  },
  {
    title: "Drop it here",
    body: "CloudLeak reads the header row, works out which cloud it came from, and maps every column to one internal schema.",
  },
  {
    title: "Read the meter",
    body: "Billed charges, evidence levels, and checks to perform before any infrastructure change.",
  },
];

export default function LandingPage() {
  return (
    <main className="measure-grid min-h-screen">
      <header className="border-b border-hairline bg-panel/70">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <span className="font-display text-lg font-700 tracking-signage">CloudLeak</span>
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-graphite">
            Multi-cloud
          </span>
        </div>
      </header>

      <div className="mx-auto max-w-6xl px-6">
        <section className="grid gap-12 py-16 lg:grid-cols-[1.05fr_1fr] lg:gap-16 lg:py-24">
          <div>
            <p className="eyebrow text-[11px] text-verdigris">
              No IAM role · No API key · No live connection
            </p>
            <h1 className="mt-5 font-display text-[clamp(2.5rem,6vw,4.25rem)] font-700 leading-[0.95] tracking-signage">
              Your cloud bill already knows where the money is going.
            </h1>
            <p className="mt-6 max-w-xl text-lg leading-relaxed text-graphite">
              Cloud cost exports can reveal charges worth investigating without granting live
              access to your infrastructure. CloudLeak separates explicitly idle billing
              signatures from charges that still require resource and dependency verification.
            </p>

            <dl className="mt-10 flex flex-wrap gap-x-10 gap-y-4 border-t border-hairline pt-6">
              <div>
                <dt className="eyebrow text-[10px] text-graphite">Clouds read</dt>
                <dd className="mt-1 font-mono text-sm">Azure · AWS · GCP</dd>
              </div>
              <div>
                <dt className="eyebrow text-[10px] text-graphite">Credentials required</dt>
                <dd className="mt-1 font-mono text-sm">None</dd>
              </div>
              <div>
                <dt className="eyebrow text-[10px] text-graphite">File retention</dt>
                <dd className="mt-1 font-mono text-sm">None</dd>
              </div>
            </dl>
          </div>

          <div className="lg:pt-8">
            <CsvUploader />
            <p className="mt-4 text-xs leading-relaxed text-graphite">
              Uploads are processed by the API and discarded from the job after parsing. Frameworks
              may use temporary storage. The resulting report stays in this browser tab.
            </p>
          </div>
        </section>

        <div className="tick-rule" />

        <section className="py-16">
          <ol className="grid gap-px bg-hairline sm:grid-cols-3">
            {STEPS.map((step, index) => (
              <li key={step.title} className="bg-concrete px-6 py-8">
                <span className="font-mono text-xs text-verdigris tabular">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <h2 className="mt-4 font-display text-xl font-600 tracking-signage">{step.title}</h2>
                <p className="mt-2 text-sm leading-relaxed text-graphite">{step.body}</p>
              </li>
            ))}
          </ol>
        </section>

        <div className="tick-rule" />

        <section className="grid gap-10 py-16 lg:grid-cols-2">
          <div>
            <h2 className="font-display text-2xl font-600 tracking-signage">What it looks for</h2>
            <ul className="mt-5 space-y-4 text-sm leading-relaxed text-graphite">
              <li>
                <strong className="font-500 text-ink">Disk capacity.</strong> Identify billed disk
                resources that warrant attachment and ownership verification.
              </li>
              <li>
                <strong className="font-500 text-ink">Idle public IPv4.</strong> Identify AWS
                idle-address billing codes; other public-IP charges need association checks.
              </li>
              <li>
                <strong className="font-500 text-ink">Untagged spend.</strong> Cost with no owner
                attached, reported separately as a governance gap rather than as waste.
              </li>
            </ul>
          </div>
          <div>
            <h2 className="font-display text-2xl font-600 tracking-signage">What it will not do</h2>
            <p className="mt-5 text-sm leading-relaxed text-graphite">
              An export cannot prove whether a disk is attached, a load balancer serves production,
              or a resource is safe to remove. CloudLeak does not claim arbitrary savings percentages
              or generate deletion scripts. A topology-aware review is required before action.
            </p>
            <Link
              href="/dashboard"
              className="mt-6 inline-block border-b border-ink font-mono text-xs uppercase tracking-[0.12em] transition-colors hover:border-verdigris hover:text-verdigris"
            >
              View the last report
            </Link>
          </div>
        </section>
      </div>

      <footer className="border-t border-hairline bg-panel">
        <div className="mx-auto max-w-6xl px-6 py-6">
          <p className="font-mono text-[11px] text-graphite">
            CloudLeak · schema modelled on the FinOps FOCUS specification
          </p>
        </div>
      </footer>
    </main>
  );
}