"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  CheckIcon,
  ChevronRightIcon,
  CopyIcon,
  KeyRoundIcon,
  Loader2Icon,
  PlusIcon,
  TriangleAlertIcon,
} from "lucide-react";

import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { SidebarMenuButton } from "@/components/ui/sidebar";

type ApiKeySummary = {
  id: number;
  name: string;
  masked_key: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  credits_allocated: number;
  credits_spent: number;
  credits_reserved: number;
  credits_available: number;
  credits_expire_at: string | null;
  credits_expired: boolean;
};

type CreditBalance = {
  key_id: number;
  allocated: number;
  spent: number;
  reserved: number;
  available: number;
  expires_at: string | null;
  expired: boolean;
};

type LedgerEntry = {
  id: number;
  kind: string;
  amount: number;
  available_after: number;
  reference: string | null;
  created_at: string;
};

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
});

const numberFormatter = new Intl.NumberFormat();

function formatDate(value: string) {
  return dateFormatter.format(new Date(value));
}

function formatCredits(value: number) {
  return numberFormatter.format(value);
}

const MS_PER_DAY = 86_400_000;

function describeExpiry(expiresAt: string | null) {
  if (!expiresAt) return "No expiry";
  const days = Math.round((new Date(expiresAt).getTime() - Date.now()) / MS_PER_DAY);
  if (days < 0) return `Expired ${formatDate(expiresAt)}`;
  if (days === 0) return "Expires today";
  return `Expires ${formatDate(expiresAt)} · ${days}d left`;
}

const LEDGER_LABELS: Record<string, string> = {
  allocate: "Top-up",
  reserve: "Reserved",
  commit: "Spent",
  release: "Released",
  expire: "Expired",
  charge: "Usage",
};

// Ledger kinds that give credits back to the available pool.
const CREDIT_GAINING_KINDS = new Set(["allocate", "release", "expire"]);

function describe(apiKey: ApiKeySummary) {
  const parts = [`Created ${formatDate(apiKey.created_at)}`];
  if (apiKey.revoked_at) {
    parts.push(`Revoked ${formatDate(apiKey.revoked_at)}`);
  } else {
    parts.push(apiKey.last_used_at ? `Last used ${formatDate(apiKey.last_used_at)}` : "Never used");
  }
  parts.push(`${formatCredits(apiKey.credits_available)} credits`);
  return parts.join(" · ");
}

// Reference call for the metered API. Same key that this dialog manages.
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const USAGE_SNIPPET = `# check a key's balance
curl ${API_BASE}/keys/<key-id>/credits

# make a metered call (charges credits for the key)
curl -X POST ${API_BASE}/usage/record \\
  -H "Authorization: Bearer <your-api-key>" \\
  -H "Content-Type: application/json" \\
  -d '{"reference":"nightly job"}'`;

function CreditsPanel({ apiKey, onChanged }: { apiKey: ApiKeySummary; onChanged: () => void }) {
  const [balance, setBalance] = useState<CreditBalance | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[] | null>(null);
  const [amount, setAmount] = useState("");
  const [ttlDays, setTtlDays] = useState("");
  const [allocating, setAllocating] = useState(false);
  const [spending, setSpending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [balanceRes, ledgerRes] = await Promise.all([
      fetch(`/api/keys/${apiKey.id}/credits`, { cache: "no-store" }),
      fetch(`/api/keys/${apiKey.id}/credits/ledger`, { cache: "no-store" }),
    ]);
    if (balanceRes.ok) setBalance(await balanceRes.json());
    if (ledgerRes.ok) setLedger(await ledgerRes.json());
  }, [apiKey.id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function readError(res: Response) {
    const data = (await res.json().catch(() => null)) as { detail?: string } | null;
    return data?.detail ?? "Request failed";
  }

  async function allocateCredits(event: FormEvent) {
    event.preventDefault();
    const parsed = Number.parseInt(amount, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      setError("Enter a positive credit amount");
      return;
    }
    const parsedTtl = ttlDays.trim() ? Number.parseInt(ttlDays, 10) : undefined;

    setAllocating(true);
    setError(null);
    try {
      const res = await fetch(`/api/keys/${apiKey.id}/credits`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: parsed, ttl_days: parsedTtl }),
      });
      if (!res.ok) {
        setError(await readError(res));
        return;
      }
      setAmount("");
      setTtlDays("");
      await Promise.all([refresh(), onChanged()]);
    } catch {
      setError("Could not add credits");
    } finally {
      setAllocating(false);
    }
  }

  async function spendCredit() {
    setSpending(true);
    setError(null);
    try {
      const res = await fetch(`/api/keys/${apiKey.id}/credits/spend`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: 1, reference: "manual spend" }),
      });
      if (!res.ok) {
        setError(await readError(res));
        return;
      }
      await Promise.all([refresh(), onChanged()]);
    } catch {
      setError("Could not spend a credit");
    } finally {
      setSpending(false);
    }
  }

  return (
    <div
      data-slot="aui-api-credits"
      className="bg-muted/40 mt-3 flex flex-col gap-3 rounded-lg border p-3"
    >
      <div className="flex items-end justify-between gap-4">
        <div className="flex flex-col gap-0.5">
          <span className="text-muted-foreground text-xs">Available</span>
          <span className="font-mono text-2xl leading-none font-semibold">
            {balance ? formatCredits(balance.available) : "—"}
          </span>
        </div>
        <dl className="text-muted-foreground flex gap-4 text-right text-xs">
          <div className="flex flex-col gap-0.5">
            <dt>Allocated</dt>
            <dd className="text-foreground font-mono">
              {balance ? formatCredits(balance.allocated) : "—"}
            </dd>
          </div>
          <div className="flex flex-col gap-0.5">
            <dt>Spent</dt>
            <dd className="text-foreground font-mono">
              {balance ? formatCredits(balance.spent) : "—"}
            </dd>
          </div>
          <div className="flex flex-col gap-0.5">
            <dt>Reserved</dt>
            <dd className="text-foreground font-mono">
              {balance ? formatCredits(balance.reserved) : "—"}
            </dd>
          </div>
        </dl>
      </div>

      <p className={cn("text-xs", balance?.expired ? "text-destructive" : "text-muted-foreground")}>
        {balance ? describeExpiry(balance.expires_at) : "Loading expiry…"}
      </p>

      <form onSubmit={allocateCredits} className="flex items-center gap-2">
        <Input
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          placeholder="Amount"
          inputMode="numeric"
          aria-label="Credits to add"
          className="w-24"
          disabled={allocating}
        />
        <Input
          value={ttlDays}
          onChange={(event) => setTtlDays(event.target.value)}
          placeholder="TTL days"
          inputMode="numeric"
          aria-label="TTL in days"
          className="w-24"
          disabled={allocating}
        />
        <Button type="submit" size="sm" disabled={allocating || !amount.trim()}>
          {allocating ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}
          Add credits
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={spendCredit}
          disabled={spending}
          className="ms-auto"
        >
          {spending ? <Loader2Icon className="animate-spin" /> : null}
          Spend 1
        </Button>
      </form>

      {error && (
        <p role="alert" className="text-destructive text-xs">
          {error}
        </p>
      )}

      <div className="flex flex-col gap-1">
        <span className="text-muted-foreground text-xs font-medium">Ledger</span>
        {ledger === null && <Skeleton className="h-10 w-full rounded-md" />}
        {ledger !== null && ledger.length === 0 && (
          <p className="text-muted-foreground text-xs">No movements yet.</p>
        )}
        {ledger !== null && ledger.length > 0 && (
          <ul className="flex flex-col gap-1">
            {ledger.slice(0, 6).map((entry) => {
              const gaining = CREDIT_GAINING_KINDS.has(entry.kind);
              return (
                <li key={entry.id} className="flex items-center gap-2 text-xs">
                  <span
                    className={cn(
                      "w-12 shrink-0 font-mono",
                      gaining ? "text-emerald-600 dark:text-emerald-400" : "text-destructive",
                    )}
                  >
                    {gaining ? "+" : "-"}
                    {entry.amount}
                  </span>
                  <span className="w-16 shrink-0">{LEDGER_LABELS[entry.kind] ?? entry.kind}</span>
                  <span className="text-muted-foreground truncate">{entry.reference ?? ""}</span>
                  <span className="text-muted-foreground ms-auto shrink-0 font-mono">
                    {entry.available_after} left
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

function ApiKeyRow({
  apiKey,
  onChanged,
  onRevoke,
}: {
  apiKey: ApiKeySummary;
  onChanged: () => void;
  onRevoke: (id: number) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const active = apiKey.revoked_at === null;

  return (
    <li
      data-slot="aui-api-key-item"
      className={cn("rounded-lg border p-3", apiKey.credits_expired && "border-destructive/40")}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium">{apiKey.name}</span>
            <span
              data-slot="aui-api-key-status"
              className={cn(
                "rounded-md px-1.5 py-0.5 text-[0.65rem] font-medium",
                active ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
              )}
            >
              {active ? "Active" : "Revoked"}
            </span>
            {active && apiKey.credits_expired && (
              <span className="bg-destructive/10 text-destructive rounded-md px-1.5 py-0.5 text-[0.65rem] font-medium">
                Credits expired
              </span>
            )}
          </div>
          <code className="text-muted-foreground font-mono text-xs">{apiKey.masked_key}</code>
          <span className="text-muted-foreground text-xs">{describe(apiKey)}</span>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {active &&
            (confirming ? (
              <>
                <span className="text-muted-foreground text-xs">Revoke?</span>
                <Button size="xs" variant="ghost" onClick={() => setConfirming(false)}>
                  Cancel
                </Button>
                <Button
                  size="xs"
                  variant="destructive"
                  onClick={() => {
                    setConfirming(false);
                    onRevoke(apiKey.id);
                  }}
                >
                  Revoke
                </Button>
              </>
            ) : (
              <Button size="sm" variant="destructive" onClick={() => setConfirming(true)}>
                Revoke
              </Button>
            ))}
          <Button
            size="icon-sm"
            variant="ghost"
            aria-expanded={expanded}
            aria-label={expanded ? "Hide credits" : "Show credits"}
            onClick={() => setExpanded((value) => !value)}
          >
            <ChevronRightIcon className={cn("transition-transform", expanded && "rotate-90")} />
          </Button>
        </div>
      </div>

      {expanded && <CreditsPanel apiKey={apiKey} onChanged={onChanged} />}
    </li>
  );
}

export function ApiKeysDialog() {
  const [open, setOpen] = useState(false);
  const [keys, setKeys] = useState<ApiKeySummary[] | null>(null);
  const [name, setName] = useState("");
  const [initialCredits, setInitialCredits] = useState("");
  const [initialTtlDays, setInitialTtlDays] = useState("");
  const [plaintextKey, setPlaintextKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { isCopied, copyToClipboard } = useCopyToClipboard();

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch("/api/keys", { cache: "no-store" });
      if (!res.ok) throw new Error(String(res.status));
      setKeys(await res.json());
    } catch {
      setKeys([]);
      setError("Could not load API keys — is the backend running?");
    }
  }, []);

  // Load lazily on first open, then refresh after every mutation.
  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  async function createKey(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || busy) return;

    const credits = initialCredits.trim() ? Number.parseInt(initialCredits, 10) : undefined;
    const ttlDays = initialTtlDays.trim() ? Number.parseInt(initialTtlDays, 10) : undefined;

    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: trimmed,
          credits: credits && credits > 0 ? credits : undefined,
          ttl_days: ttlDays && ttlDays > 0 ? ttlDays : undefined,
        }),
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = (await res.json()) as { plaintext_key: string };
      setPlaintextKey(data.plaintext_key);
      setName("");
      setInitialCredits("");
      setInitialTtlDays("");
      await load();
    } catch {
      setError("Could not create the key. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: number) {
    setError(null);
    try {
      const res = await fetch(`/api/keys/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(String(res.status));
      await load();
    } catch {
      setError("Could not revoke the key. Please try again.");
    }
  }

  const activeKeys = keys?.filter((k) => k.revoked_at === null) ?? [];
  const totalAvailable = activeKeys.reduce((sum, k) => sum + k.credits_available, 0);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<SidebarMenuButton size="lg" data-slot="aui-api-keys-trigger" />}>
        <div className="aui-api-keys-trigger-icon bg-sidebar-primary text-sidebar-primary-foreground flex aspect-square size-8 items-center justify-center rounded-lg">
          <KeyRoundIcon className="aui-api-keys-trigger-glyph size-4" />
        </div>
        <div className="aui-api-keys-trigger-heading flex flex-col gap-0.5 leading-none">
          <span className="aui-api-keys-trigger-title font-semibold">API keys</span>
          <span className="aui-api-keys-trigger-subtitle text-muted-foreground">
            {keys === null
              ? "Create & revoke"
              : `${activeKeys.length} active · ${formatCredits(totalAvailable)} credits`}
          </span>
        </div>
      </DialogTrigger>

      <DialogContent
        data-slot="aui-api-keys-dialog"
        className="max-h-[85dvh] gap-0 overflow-y-auto sm:max-w-lg"
      >
        <DialogHeader data-slot="aui-api-keys-header">
          <DialogTitle>API keys</DialogTitle>
          <DialogDescription>
            Bearer tokens authenticate requests to the miniLLM API. Each key spends credits, which
            expire — usage is blocked once a key runs out. A new key is shown only once, so copy it
            and store it somewhere safe.
          </DialogDescription>
        </DialogHeader>

        <div className="mt-4 flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <form onSubmit={createKey} className="flex items-center gap-2">
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Key name — e.g. production app"
                maxLength={100}
                aria-label="Key name"
                disabled={busy}
              />
              <Button type="submit" disabled={busy || !name.trim()}>
                {busy ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}
                {busy ? "Creating…" : "Create key"}
              </Button>
            </form>
            <div className="grid grid-cols-2 gap-2">
              <Input
                value={initialCredits}
                onChange={(event) => setInitialCredits(event.target.value)}
                placeholder="Initial credits (optional)"
                inputMode="numeric"
                aria-label="Initial credits"
                disabled={busy}
              />
              <Input
                value={initialTtlDays}
                onChange={(event) => setInitialTtlDays(event.target.value)}
                placeholder="TTL days (optional)"
                inputMode="numeric"
                aria-label="Initial TTL in days"
                disabled={busy}
              />
            </div>
          </div>

          {error && (
            <p role="alert" className="text-destructive text-sm">
              {error}
            </p>
          )}

          {plaintextKey && (
            <section
              data-slot="aui-api-keys-new"
              className="border-destructive/30 bg-destructive/5 flex flex-col gap-2 rounded-lg border p-3"
            >
              <p className="flex items-center gap-2 text-sm font-medium">
                <TriangleAlertIcon className="text-destructive size-4 shrink-0" />
                Copy this key now — it will not be shown again.
              </p>
              <code className="bg-background block rounded-md border p-2 font-mono text-xs break-all">
                {plaintextKey}
              </code>
              <div className="flex gap-2">
                <Button size="sm" variant="secondary" onClick={() => copyToClipboard(plaintextKey)}>
                  {isCopied ? <CheckIcon /> : <CopyIcon />}
                  {isCopied ? "Copied" : "Copy"}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setPlaintextKey(null)}>
                  Done
                </Button>
              </div>
            </section>
          )}

          <div className="flex flex-col gap-2">
            <span className="text-muted-foreground text-xs font-medium">
              {keys === null
                ? "Loading keys…"
                : `${keys.length} key${keys.length === 1 ? "" : "s"}`}
            </span>

            {keys === null && (
              <div className="flex flex-col gap-2">
                {Array.from({ length: 2 }, (_, index) => (
                  <Skeleton key={index} className="h-16 w-full rounded-lg" />
                ))}
              </div>
            )}

            {keys !== null && keys.length === 0 && (
              <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-center text-sm">
                No keys yet. Create one to start calling the API.
              </p>
            )}

            {keys !== null && keys.length > 0 && (
              <ul className="flex flex-col gap-2">
                {keys.map((apiKey) => (
                  <ApiKeyRow key={apiKey.id} apiKey={apiKey} onChanged={load} onRevoke={revoke} />
                ))}
              </ul>
            )}
          </div>

          <div className="bg-muted/50 rounded-lg border p-3">
            <p className="text-muted-foreground mb-1.5 text-xs font-medium">Usage</p>
            <pre className="text-muted-foreground overflow-x-auto font-mono text-[0.7rem] leading-relaxed">
              <code>{USAGE_SNIPPET}</code>
            </pre>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
