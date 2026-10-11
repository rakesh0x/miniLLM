"use client";

import { useEffect, useState } from "react";

/**
 * Plaintext API keys the user has chosen to chat with.
 *
 * The backend only ever stores a key's hash, so the plaintext cannot be
 * recovered after the API keys dialog closes. To let the chat header send a
 * bearer token, the keys the user opts in with are kept here — in this
 * browser only. The alternative is pasting the key on every send.
 */

export type ChatKey = {
  id: number;
  name: string;
  /** The full `mnl_...` secret. Never leaves this browser except as a header. */
  plaintext: string;
  savedAt: number;
};

const STORAGE_KEY = "minillm.chat.keys.v1";
const SELECTED_KEY = "minillm.chat.selected.v1";
const CHANGE_EVENT = "minillm:chat-keys-changed";

function isChatKey(value: unknown): value is ChatKey {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "number" &&
    typeof record.name === "string" &&
    typeof record.plaintext === "string"
  );
}

export function loadChatKeys(): ChatKey[] {
  if (typeof window === "undefined") return [];
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isChatKey);
  } catch {
    return [];
  }
}

export function saveChatKey(key: ChatKey): ChatKey[] {
  const keys = loadChatKeys().filter((existing) => existing.id !== key.id);
  keys.unshift(key);
  persist(keys);
  return keys;
}

export function removeChatKey(id: number): ChatKey[] {
  persist(loadChatKeys().filter((key) => key.id !== id));
  return loadChatKeys();
}

function persist(keys: ChatKey[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(keys));
    window.dispatchEvent(new Event(CHANGE_EVENT));
  } catch {
    // Storage full or blocked: the picker still works for this session.
  }
}

export function loadSelectedKeyId(): number | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(SELECTED_KEY);
  const id = raw === null ? Number.NaN : Number.parseInt(raw, 10);
  return Number.isFinite(id) ? id : null;
}

export function saveSelectedKeyId(id: number | null): void {
  if (typeof window === "undefined") return;
  try {
    if (id === null) window.localStorage.removeItem(SELECTED_KEY);
    else window.localStorage.setItem(SELECTED_KEY, String(id));
  } catch {
    // Selection simply is not remembered across reloads.
  }
}

/** Subscribes to changes made elsewhere (e.g. the API keys dialog). */
export function useChatKeys(): ChatKey[] {
  const [keys, setKeys] = useState<ChatKey[]>([]);

  useEffect(() => {
    const sync = () => setKeys(loadChatKeys());
    sync();
    window.addEventListener(CHANGE_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(CHANGE_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  return keys;
}
