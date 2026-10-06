import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, SetupStatus, Site } from "../api";
import { DefaultsForm } from "../components/DefaultsForm";
import { DiscoveryPanel } from "../components/Discovery";
import { Field, Notice } from "../components/Field";
import { SiteForm } from "../components/SiteForm";
import { LoginForm } from "./Login";

const STEPS = ["Welcome", "Administrator", "Defaults", "First site", "Discovery", "Finish"] as const;
type Step = (typeof STEPS)[number];

function firstOpenStep(s: SetupStatus): Step {
  if (!s.admin_exists) return "Welcome";
  if (!s.steps) return "Administrator";
  if (!s.steps.defaults) return "Defaults";
  if (!s.steps.site) return "First site";
  if (!s.steps.discovery) return "Discovery";
  return "Finish";
}

/**
 * First-run setup, shown until an administrator finishes it. Everything is
 * entered here in the browser; infrastructure secrets were generated
 * automatically when the stack first started.
 */
export function SetupWizard({ status, refresh }: { status: SetupStatus; refresh: () => Promise<void> }) {
  const [step, setStep] = useState<Step>(() => firstOpenStep(status));
  const [site, setSite] = useState<Site | null>(null);

  useEffect(() => {
    if (status.steps?.site && !site) {
      api.listSites().then((s) => s.length && setSite(s[0])).catch(() => undefined);
    }
  }, [status.steps?.site]);

  // An administrator exists but this browser is not signed in.
  if (status.admin_exists && !status.user) {
    return (
      <Shell step="Administrator">
        <h2>Sign in to continue setup</h2>
        <p>Setup has started. Sign in with the administrator account created earlier.</p>
        <LoginForm onDone={refresh} />
      </Shell>
    );
  }
  if (status.user && status.user.role !== "admin") {
    return (
      <Shell step="Welcome">
        <Notice kind="warn">Setup is not finished yet. An administrator needs to complete it.</Notice>
      </Shell>
    );
  }

  const go = async (next: Step) => {
    await refresh();
    setStep(next);
  };

  return (
    <Shell step={step}>
      {step === "Welcome" && <Welcome status={status} onNext={() => setStep("Administrator")} />}
      {step === "Administrator" && (
        <>
          <h2>Create the administrator account</h2>
          <p>
            This local account can always sign in, even if single sign-on is unavailable, so keep its
            password somewhere safe. More users (and Microsoft sign-on) are added later in the admin panel.
          </p>
          {status.admin_exists ? (
            <>
              <Notice kind="ok">Administrator account created: {status.user?.username}</Notice>
              <div className="actions"><button onClick={() => setStep("Defaults")}>Next</button></div>
            </>
          ) : status.admin_window_open ? (
            <AdminForm onDone={() => go("Defaults")} />
          ) : (
            <Notice kind="error">
              The first-run window has closed (it stays open for 60 minutes after the system starts, so that
              nobody else on the network can claim a new install). Restart the <code>api</code> container from
              your Docker management tool to reopen it.
            </Notice>
          )}
        </>
      )}
      {step === "Defaults" && (
        <>
          <h2>Defaults for all sites</h2>
          <p>
            These suggested values apply to every site unless a site overrides them. You can change them at any
            time in the admin panel.
          </p>
          <DefaultsForm submitLabel="Save and continue" onSaved={() => go("First site")} />
        </>
      )}
      {step === "First site" && (
        <>
          <h2>Add the first grain site</h2>
          <p>
            Enter the connection to one site's CompuWeigh SQL Server. Use <strong>Test connection</strong> before
            saving. The site is saved with polling off; nothing is collected until its mapping is confirmed.
          </p>
          <PrepNotes />
          <SiteForm
            site={site ?? undefined}
            submitLabel="Save and continue"
            onSaved={async (s) => {
              setSite(s);
              await go("Discovery");
            }}
          />
        </>
      )}
      {step === "Discovery" && (
        <>
          <h2>Discover the scale database</h2>
          {site ? (
            <DiscoveryPanel siteId={site.id} onFinished={() => refresh()} />
          ) : (
            <Notice kind="warn">Add a site first.</Notice>
          )}
          <div className="actions">
            <button className="secondary" onClick={() => setStep("First site")}>Back</button>
            <button onClick={() => go("Finish")}>{status.steps?.discovery ? "Next" : "Skip for now"}</button>
          </div>
        </>
      )}
      {step === "Finish" && (
        <>
          <h2>Setup complete</h2>
          <ul>
            <li>Administrator account created</li>
            <li>Defaults saved</li>
            <li>{site ? `Site “${site.name}” added (polling off)` : "No site added yet"}</li>
            <li>{status.steps?.discovery ? "Discovery report ready to download" : "Discovery not run yet"}</li>
          </ul>
          <p>
            Next: send the discovery report to the project team. Once the mapping is confirmed, polling is turned on
            for the site from the admin panel. Everything else, including adding more sites, happens in the admin
            panel.
          </p>
          <div className="actions">
            <button
              onClick={async () => {
                await api.completeSetup();
                await refresh();
              }}
            >
              Open the admin panel
            </button>
          </div>
        </>
      )}
    </Shell>
  );
}

function Shell({ step, children }: { step: Step; children: React.ReactNode }) {
  const idx = STEPS.indexOf(step);
  return (
    <div className="setup">
      <header className="setup-header">
        <h1>GrainTime setup</h1>
        <p className="muted">Truck time on site, for Mercer Landmark grain elevators</p>
      </header>
      <ol className="stepper">
        {STEPS.map((s, i) => (
          <li key={s} className={i < idx ? "done" : i === idx ? "current" : ""}>{s}</li>
        ))}
      </ol>
      <main className="card">{children}</main>
    </div>
  );
}

function Welcome({ status, onNext }: { status: SetupStatus; onNext: () => void }) {
  return (
    <>
      <h2>Welcome</h2>
      <p>This wizard takes about ten minutes. Everything is entered here — nothing needs a terminal or a config file.</p>
      <ol>
        <li>Create the administrator account.</li>
        <li>Confirm the default thresholds and timings.</li>
        <li>Add the first grain site and test its connection.</li>
        <li>Run discovery on it, so its scale database can be mapped.</li>
      </ol>
      <Notice kind="info">
        Already done automatically: the central database password and the key that encrypts site passwords were
        generated on first start and stored in the stack's secrets volume. Include that volume in your backups.
      </Notice>
      {status.admin_window_open && status.admin_window_closes_at ? (
        <p className="muted small">
          For safety, the administrator account must be created before{" "}
          {new Date(status.admin_window_closes_at).toLocaleTimeString()}.
        </p>
      ) : null}
      <div className="actions"><button onClick={onNext}>Start</button></div>
    </>
  );
}

function AdminForm({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("admin");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    setErrors({});
    setMessage(null);
    if (password !== confirm) {
      setErrors({ confirm: "The passwords do not match." });
      return;
    }
    setBusy(true);
    try {
      await api.createAdmin({ username, display_name: displayName || username, password });
      onDone();
    } catch (e) {
      if (e instanceof ApiError) {
        setErrors(e.fields);
        setMessage(e.message);
      } else setMessage("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="form narrow" onSubmit={submit} noValidate>
      <Field label="Username" error={errors.username}>
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
      </Field>
      <Field label="Your name" error={errors.display_name}>
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
      </Field>
      <Field label="Password" error={errors.password} hint="At least 12 characters">
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
      </Field>
      <Field label="Confirm password" error={errors.confirm}>
        <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
      </Field>
      {message ? <Notice kind="error">{message}</Notice> : null}
      <div className="actions"><button type="submit" disabled={busy}>Create administrator</button></div>
    </form>
  );
}

function PrepNotes() {
  return (
    <details className="prep">
      <summary>Before you start: preparing the site's SQL Server</summary>
      <ol className="small">
        <li>Enable <strong>TCP/IP</strong> for the instance in SQL Server Configuration Manager (off by default on Express).</li>
        <li>Give the instance a <strong>static TCP port</strong> (IP Addresses → IPAll → TCP Port; clear “TCP Dynamic Ports”), then restart the SQL Server service.</li>
        <li>Turn on <strong>SQL Server and Windows Authentication mode</strong> (Server Properties → Security), then restart the service.</li>
        <li>Add a <strong>Windows Firewall</strong> inbound rule at the site for that TCP port from this server.</li>
        <li>
          Create a read-only SQL login for GrainTime. For discovery, membership in <code>db_datareader</code> on the
          scale database is enough; it is replaced by a narrower login once the mapping is confirmed.
        </li>
      </ol>
    </details>
  );
}
