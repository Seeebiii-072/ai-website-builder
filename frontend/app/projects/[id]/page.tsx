'use client';

import { useEffect, useRef, useState, use as usePromise } from 'react';
import { api, ProjectResponse } from '@/lib/api';
import TopBar from '@/components/TopBar';
import ChatPanel, { ChatMessage } from '@/components/ChatPanel';
import PreviewPanel from '@/components/PreviewPanel';
import FileExplorer from '@/components/FileExplorer';

let msgCounter = 0;
function nextId() {
  msgCounter += 1;
  return `m${msgCounter}`;
}

export default function ProjectPage({ params }: { params: { id: string } }) {
  const projectId = params.id;
  const [project, setProject] = useState<ProjectResponse | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [fileRefreshKey, setFileRefreshKey] = useState(0);
  const [busy, setBusy] = useState(true);
  const sourceRef = useRef<EventSource | null>(null);

  function pushMessage(role: ChatMessage['role'], text: string, isError = false) {
    setMessages((prev) => [...prev, { id: nextId(), role, text, isError }]);
  }

  useEffect(() => {
    let ignore = false; // guards against React 18 dev-mode double-invoke of this effect

    api.getProject(projectId).then((p) => {
      if (ignore) return;
      setProject(p);
      pushMessage('user', p.prompt);
      if (p.status === 'READY' || p.status === 'FAILED') setBusy(false);
    });

    const es = new EventSource(api.eventsUrl(projectId));
    sourceRef.current = es;

    // Safety-net polling: SSE carries a short replay buffer so a browser that
    // connects late still gets every event that happened since project
    // creation, but if the EventSource itself never connects (proxy, CORS,
    // firewall) the UI would otherwise wait forever. Poll as a fallback while
    // the project hasn't reached a terminal state.
    const pollInterval = setInterval(() => {
      if (ignore) return;
      api
        .getProject(projectId)
        .then((p) => {
          if (ignore) return;
          setProject(p);
          if (p.status === 'READY' || p.status === 'FAILED') {
            setBusy(false);
            clearInterval(pollInterval);
          }
        })
        .catch(() => {});
    }, 4000);

    const handlers: Record<string, (data: any) => void> = {
      generation_started: () => pushMessage('ai', 'Generating website with AI…'),
      generation_completed: (d) => pushMessage('ai', `✓ Code generated (via ${d.provider})`),
      generation_failed: (d) => {
        pushMessage('ai', `AI generation failed.\n${d.error}`, true);
        setBusy(false);
      },
      build_started: () => pushMessage('ai', 'Building the project…'),
      build_failed: (d) =>
        pushMessage(
          'ai',
          d.stage === 'install'
            ? `Dependency installation failed.`
            : `Build failed (attempt ${d.attempt}/${d.max_attempts}). AI is attempting to fix the code…`,
          true
        ),
      build_completed: () => pushMessage('ai', '✓ Build succeeded'),
      ai_fix_started: (d) => pushMessage('ai', `Attempting automatic fix (attempt ${d.attempt})…`),
      ai_fix_completed: (d) => pushMessage('ai', `✓ Applied fix to: ${d.files_changed.join(', ')}`),
      ai_fix_failed: (d) => pushMessage('ai', `Automatic fix failed: ${d.error}`, true),
      preview_starting: () => pushMessage('ai', 'Starting live preview server…'),
      preview_ready: (d) => {
        pushMessage('ai', `✓ Preview ready at ${d.url}`);
        setBusy(false);
      },
      preview_failed: (d) => {
        pushMessage('ai', `Preview failed to start: ${d.error}`, true);
        setBusy(false);
      },
      preview_stopped: () => {},
      ai_edit_started: (d) => pushMessage('ai', `Applying edit: "${d.message}"`),
      ai_edit_completed: (d) => pushMessage('ai', `✓ Updated: ${d.files_changed.join(', ')}`),
      ai_edit_failed: (d) => {
        pushMessage('ai', `Edit failed: ${d.error}`, true);
        setBusy(false);
      },
    };

    es.onmessage = () => {};
    Object.entries(handlers).forEach(([eventName, handler]) => {
      es.addEventListener(eventName, (e: MessageEvent) => {
        if (ignore) return;
        const payload = JSON.parse(e.data);
        handler(payload.data);
        setFileRefreshKey((k) => k + 1);
        api.getProject(projectId).then((p) => !ignore && setProject(p)).catch(() => {});
      });
    });

    return () => {
      ignore = true;
      clearInterval(pollInterval);
      es.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function handleSend(text: string) {
    pushMessage('user', text);
    setBusy(true);
    try {
      await api.edit(projectId, text);
    } catch (e: any) {
      pushMessage('ai', `Failed to send edit: ${e.message}`, true);
      setBusy(false);
    }
  }

  if (!project) {
    return <div className="min-h-screen flex items-center justify-center text-neutral-500">Loading…</div>;
  }

  return (
    <div className="h-screen flex flex-col">
      <TopBar name={project.name} status={project.status} projectId={projectId} />
      <div className="flex-1 grid" style={{ gridTemplateColumns: '25% 50% 25%' }}>
        <ChatPanel messages={messages} onSend={handleSend} disabled={busy} />
        <PreviewPanel
          previewUrl={project.preview_url}
          status={project.status}
          onRefresh={() => api.getProject(projectId).then(setProject)}
        />
        <FileExplorer projectId={projectId} refreshKey={fileRefreshKey} />
      </div>
    </div>
  );
}
