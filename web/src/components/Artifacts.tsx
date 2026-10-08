import { useCallback, useEffect, useState } from 'react';
import { api, authHeaders } from '../api';
import type { components } from '../generated/openapi';

type Artifact = components['schemas']['ArtifactSummary'];
type Version = components['schemas']['ArtifactOut'];
type Diff = components['schemas']['ArtifactDiff'];
type Rubric = components['schemas']['RubricOut'];

const IMAGES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml']);

function query(params: Record<string, string>): string {
  return new URLSearchParams(params).toString();
}

export function Artifacts(props: {
  sid: string;
  refresh: number;
  onActivate: (rubricId: string) => Promise<boolean>;
  onProblem: (error: unknown) => void;
}) {
  const base = `/api/v1/sessions/${encodeURIComponent(props.sid)}`;
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [version, setVersion] = useState<string | null>(null);
  const [preview, setPreview] = useState('');
  const [image, setImage] = useState<string | null>(null);
  const [diff, setDiff] = useState<Diff | null>(null);
  const [rubrics, setRubrics] = useState<Rubric[]>([]);
  const [feedback, setFeedback] = useState('');
  const [lines, setLines] = useState('');

  const loadRubrics = useCallback(async () => {
    const listing = await api<{ rubrics: Rubric[] }>(`${base}/rubrics`);
    setRubrics(listing.rubrics);
  }, [base]);

  useEffect(() => {
    void api<{ artifacts: Artifact[] }>(`${base}/artifacts`)
      .then((listing) => setArtifacts(listing.artifacts))
      .then(loadRubrics)
      .catch(props.onProblem);
  }, [base, props.refresh, props.onProblem, loadRubrics]);

  async function open(id: string) {
    setSelected(id);
    setDiff(null);
    const listing = await api<{ versions: Version[] }>(`${base}/artifacts/versions?${query({ id })}`);
    setVersions(listing.versions);
    const latest = listing.versions[listing.versions.length - 1];
    if (latest) await show(id, latest);
  }

  async function show(id: string, chosen: Version) {
    setVersion(chosen.version);
    setImage(null);
    if (IMAGES.has(chosen.mime)) {
      // 原文是附件、按 octet-stream 交出；只有图片才按登记的 mime 包成 blob 显示（<img> 不执行脚本）。
      const response = await fetch(`${base}/artifacts/content?${query({ id, version: chosen.version })}`, {
        headers: authHeaders(),
      });
      const blob = new Blob([await response.arrayBuffer()], { type: chosen.mime });
      setImage(URL.createObjectURL(blob));
      setPreview('');
      return;
    }
    const shown = await api<{ text: string }>(`${base}/artifacts/preview?${query({ id, version: chosen.version })}`);
    setPreview(shown.text);
  }

  async function compare(old: string, current: string) {
    if (!selected) return;
    setDiff(await api<Diff>(`${base}/artifacts/diff?${query({ id: selected, old, new: current })}`));
  }

  async function download() {
    if (!selected || !version) return;
    const response = await fetch(`${base}/artifacts/content?${query({ id: selected, version })}`, {
      headers: authHeaders(),
    });
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = selected.split(/[:/]/).pop() ?? 'artifact';
    link.click();
    URL.revokeObjectURL(url);
  }

  async function submitFeedback() {
    if (!selected || !version) return;
    const match = /^(\d+)(?:-(\d+))?$/.exec(lines.trim());
    const body: Record<string, unknown> = { id: selected, version, text: feedback };
    if (match) {
      body.start_line = Number(match[1]);
      if (match[2]) body.end_line = Number(match[2]);
    }
    try {
      await api(`${base}/artifacts/feedback`, { method: 'POST', body: JSON.stringify(body) });
      setFeedback('');
      setLines('');
      await loadRubrics();
    } catch (error) {
      props.onProblem(error);
    }
  }

  return (
    <section className="artifacts" aria-label="产物">
      {artifacts.length === 0 && <p className="empty">还没有登记过产物（装了 acceptance 的会话才登记）。</p>}
      <ul className="artifact-list">
        {artifacts.map((artifact) => (
          <li key={artifact.id}>
            <button type="button" onClick={() => void open(artifact.id).catch(props.onProblem)}>
              {artifact.path}
            </button>{' '}
            <span className="meta">
              {artifact.is_deliverable ? '交付物' : '中间产物'} · {artifact.versions} 个版本
            </span>
            {artifact.note && (
              <p className="note untrusted" data-testid="artifact-note">
                交付说明（模型写的）：{artifact.note}
              </p>
            )}
          </li>
        ))}
      </ul>
      {selected && (
        <div className="artifact-detail">
          <div className="versions">
            {versions.map((item, index) => (
              <button
                type="button"
                key={item.version}
                aria-pressed={item.version === version}
                onClick={() => void show(selected, item).catch(props.onProblem)}
              >
                v{index + 1}
              </button>
            ))}
            {versions.length >= 2 && (
              <button
                type="button"
                onClick={() => {
                  const old = versions[versions.length - 2];
                  const current = versions[versions.length - 1];
                  if (old && current) void compare(old.version, current.version).catch(props.onProblem);
                }}
              >
                对比最近两版
              </button>
            )}
            <button type="button" onClick={() => void download().catch(props.onProblem)}>
              下载
            </button>
          </div>
          {image ? <img className="preview-image" src={image} alt={selected} /> : <pre className="preview">{preview}</pre>}
          {diff && (
            <div className="diff">
              <p>{diff.summary}</p>
              <pre>{diff.unified}</pre>
            </div>
          )}
          <form
            className="feedback"
            onSubmit={(event) => {
              event.preventDefault();
              void submitFeedback();
            }}
          >
            <textarea
              aria-label="对这个产物的反馈"
              value={feedback}
              onChange={(event) => setFeedback(event.target.value)}
              placeholder="哪里不对、应该是什么"
            />
            <input
              aria-label="行范围"
              value={lines}
              onChange={(event) => setLines(event.target.value)}
              placeholder="行，如 3 或 3-5（可不填）"
            />
            <button type="submit">提交反馈</button>
          </form>
        </div>
      )}
      {rubrics.length > 0 && (
        <ul className="rubrics" aria-label="验收标准">
          {rubrics.map((rubric) => (
            <li key={rubric.id} data-status={rubric.status}>
              <span className="status">{rubric.status === 'proposed' ? '提议' : '生效'}</span> {rubric.statement}{' '}
              {rubric.status === 'proposed' && (
                <button
                  type="button"
                  onClick={() =>
                    void props.onActivate(rubric.id).then(async (ok) => {
                      if (ok) await loadRubrics();
                    })
                  }
                >
                  生效
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
