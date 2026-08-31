import ImportWizard from "@/components/ImportWizard";

export const dynamic = "force-dynamic";

export default function ImportPage() {
  return (
    <>
      <h1>Import data</h1>
      <p className="subtitle">
        Nothing needs to be installed in your systems. Export what already exists and the
        process is reconstructed from it.
      </p>
      <ImportWizard />
    </>
  );
}
