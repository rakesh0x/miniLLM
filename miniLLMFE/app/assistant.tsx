"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useChatRuntime, AssistantChatTransport } from "@assistant-ui/ai-sdk";
import { lastAssistantMessageIsCompleteWithToolCalls } from "ai";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { ThreadListSidebar } from "@/components/assistant-ui/elements/threadlist-sidebar.aui";
import { Separator } from "@/components/ui/separator";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { ChatKeyPicker } from "@/components/assistant-ui/elements/chat-key-picker";
import { loadChatKeys, loadSelectedKeyId } from "@/lib/chat-key-store";

export const Assistant = () => {
  // The key a chat message is billed to. Held in a ref so the transport can
  // read the current key per request without being rebuilt.
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const keyRef = useRef<string | null>(null);
  keyRef.current = selectedKey;

  // Restore the previous selection once, from keys saved in this browser.
  useEffect(() => {
    const id = loadSelectedKeyId();
    if (id === null) return;
    const saved = loadChatKeys().find((key) => key.id === id);
    if (saved) setSelectedKey(saved.plaintext);
  }, []);

  const handleSelectKey = useCallback((key: string | null) => {
    setSelectedKey(key);
    keyRef.current = key;
  }, []);

  const runtime = useChatRuntime({
    sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithToolCalls,
    transport: new AssistantChatTransport({
      api: "/api/chat",
      headers: () => {
        console.log("Api key available", Boolean(keyRef.current))
        const headers: Record<string, string> = {
          "Content-Type": "application/json"
        };

        if (keyRef.current) headers.Authorization = `Bearer ${keyRef.current}`;
        return headers;
      },
    }),
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <SidebarProvider>
        <div className="flex h-dvh w-full pr-0.5">
          <ThreadListSidebar />
          <SidebarInset>
            <header className="flex h-16 shrink-0 items-center gap-2 border-b px-4">
              <SidebarTrigger />
              <Separator orientation="vertical" className="mr-2 h-4" />
              <Breadcrumb>
                <BreadcrumbList>
                  <BreadcrumbItem className="hidden md:block">
                    <BreadcrumbLink
                      href="https://www.assistant-ui.com/docs/getting-started"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      Build Your Own ChatGPT UX
                    </BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator className="hidden md:block" />
                  <BreadcrumbItem>
                    <BreadcrumbPage>miniLLM</BreadcrumbPage>
                  </BreadcrumbItem>
                </BreadcrumbList>
              </Breadcrumb>
              <div className="ms-auto">
                <ChatKeyPicker selectedKey={selectedKey} onSelectKey={handleSelectKey} />
              </div>
            </header>
            <div className="flex-1 overflow-hidden">
              <Thread />
            </div>
          </SidebarInset>
        </div>
      </SidebarProvider>
    </AssistantRuntimeProvider>
  );
};
