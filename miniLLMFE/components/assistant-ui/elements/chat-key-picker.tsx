"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  KeyRoundIcon,
  PlusIcon,
  TriangleAlertIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  removeChatKey,
  saveChatKey,
  saveSelectedKeyId,
  useChatKeys,
  type ChatKey,
} from "@/lib/chat-key-store";

type KeySummary = {
  id: number;
  name: string;
  masked_key: string;
  revoked_at: string | null;
  credits_available: number;
  credits_expired: boolean;
};

type ChatKeyPickerProps = {
  /** The plaintext key sent as `Authorization: Bearer ...`, or null. */
  selectedKey: string | null;
  onSelectKey: (key: string | null) => void;
};

/**
 * Picks which miniLLM API key a chat message is billed to.
 *
 * The backend authenticates on the raw key and only stores its hash, so this
 * lists keys the user has opted into (saved from the API keys dialog, or
 * pasted) alongside each key's live balance from `/api/keys`. Selecting one
 * sends it as the bearer token on every chat request.
 */
export function ChatKeyPicker({ selectedKey, onSelectKey }: ChatKeyPickerProps) {
  const stored = useChatKeys();
  const [summaries, setSummaries] = useState<KeySummary[] | null>(null);
  const [open, setOpen] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/keys", { cache: "no-store" });
      if (response.ok) setSummaries((await response.json()) as KeySummary[]);
      else setSummaries([]);
    } catch {
      setSummaries([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const selected = useMemo(
    () => stored.find((key) => key.plaintext === selectedKey) ?? null,
    [stored, selectedKey],
  );
  const selectedSummary = selected
    ? (summaries?.find((summary) => summary.id === selected.id) ?? null)
    : null;

  const activeKeys = useMemo(
    () => (summaries ?? []).filter((summary) => summary.revoked_at === null),
    [summaries],
  );

  function select(key: ChatKey) {
    onSelectKey(key.plaintext);
    saveSelectedKeyId(key.id);
    setOpen(false);
    setPasting(false);
    setError(null);
  }

  function forget(key: ChatKey) {
    removeChatKey(key.id);
    if (selected?.id === key.id) {
      onSelectKey(null);
      saveSelectedKeyId(null);
    }
    setOpen(false);
  }

  function addKey() {
    const plaintext = draft.trim();
    if (!plaintext.startsWith("mnl_")) {
      setError("A miniLLM key starts with mnl_");
      return;
    }
    const prefix = plaintext.slice(0, 4);
    const suffix = plaintext.slice(-4);
    // `/keys` only exposes the masked form, so match on prefix + last four.
    const match = activeKeys.find(
      (summary) => summary.masked_key.startsWith(prefix) && summary.masked_key.endsWith(suffix),
    );
    const entry: ChatKey = {
      id: match?.id ?? -1,
      name: match?.name ?? "Pasted key",
      plaintext,
      savedAt: Date.now(),
    };
    saveChatKey(entry);
    onSelectKey(plaintext);
    saveSelectedKeyId(entry.id);
    setDraft("");
    setOpen(false);
    setPasting(false);
    setError(null);
  }

  const label = selected ? selected.name : "No API key";
  const subtitle = selected
    ? selectedSummary
      ? selectedSummary.credits_expired
        ? "Credits expired"
        : `${selectedSummary.credits_available} credits`
      : "Balance unknown"
    : "Pick a key to chat";

  return (
    <div ref={containerRef} className="relative">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-haspopup="menu"
        aria-expanded={open}
        data-slot="aui-chat-key-picker"
        onClick={() => {
          setOpen((value) => !value);
          if (!open) void refresh();
        }}
        className={cn("gap-2", !selected && "text-muted-foreground")}
      >
        <KeyRoundIcon className="size-4" />
        <span className="hidden flex-col items-start sm:flex">
          <span className="text-xs leading-tight font-medium">{label}</span>
          <span className="text-muted-foreground text-[0.65rem] leading-tight">{subtitle}</span>
        </span>
        <ChevronDownIcon className="size-3.5" />
      </Button>

      {open && (
        <div
          role="menu"
          data-slot="aui-chat-key-menu"
          className="bg-popover text-popover-foreground absolute right-0 z-50 mt-2 w-72 rounded-lg border p-1 shadow-md"
        >
          {!selected && (
            <p className="text-muted-foreground m-1 flex items-start gap-2 text-xs">
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              Chat requests are billed to a key. Pick one below, or paste a key.
            </p>
          )}

          {stored.length === 0 && !pasting && (
            <p className="text-muted-foreground m-1 text-xs">
              No keys saved yet. Create one in the API keys dialog, or paste an existing key.
            </p>
          )}

          {stored.map((key) => {
            const summary = summaries?.find((entry) => entry.id === key.id);
            const revoked = summary?.revoked_at != null;
            const isSelected = selected?.plaintext === key.plaintext;
            return (
              <div key={key.id} className="flex items-center gap-1">
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => select(key)}
                  disabled={revoked}
                  className={cn(
                    "hover:bg-accent flex flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs",
                    revoked && "opacity-50",
                  )}
                >
                  <CheckIcon
                    className={cn("size-3.5 shrink-0", isSelected ? "opacity-100" : "opacity-0")}
                  />
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate font-medium">{key.name}</span>
                    <span className="text-muted-foreground truncate">
                      {revoked
                        ? "Revoked"
                        : summary
                          ? `${summary.credits_available} credits`
                          : "Balance unknown"}
                    </span>
                  </span>
                </button>
                <Button
                  type="button"
                  variant="ghost"
                  size="xs"
                  onClick={() => forget(key)}
                  aria-label={`Forget ${key.name}`}
                >
                  Forget
                </Button>
              </div>
            );
          })}

          <div className="mt-1 border-t pt-1">
            {pasting ? (
              <div className="flex flex-col gap-2 p-1">
                <Input
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      addKey();
                    }
                  }}
                  placeholder="mnl_..."
                  aria-label="Paste an API key"
                  className="font-mono text-xs"
                  autoFocus
                />
                {error && (
                  <p role="alert" className="text-destructive text-xs">
                    {error}
                  </p>
                )}
                <div className="flex gap-1">
                  <Button type="button" size="xs" onClick={addKey}>
                    Use this key
                  </Button>
                  <Button
                    type="button"
                    size="xs"
                    variant="ghost"
                    onClick={() => {
                      setPasting(false);
                      setError(null);
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                role="menuitem"
                onClick={() => setPasting(true)}
                className="hover:bg-accent flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs"
              >
                <PlusIcon className="size-3.5" />
                Paste another key
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
