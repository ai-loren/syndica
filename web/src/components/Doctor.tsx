import type { components } from '../generated/openapi';

type Report = components['schemas']['DoctorReport'];

export function Doctor({ report }: { report: Report | null }) {
  if (!report || report.ok) return null;
  return (
    <section className="doctor" aria-label="自检">
      <h2>自检没有通过</h2>
      {report.checks
        .filter((check) => !check.ok)
        .map((check) => (
          <div className="problem" role="alert" key={check.name}>
            <strong>{check.name}</strong>
            <dl>
              <dt>期望</dt>
              <dd>{check.expected}</dd>
              <dt>实际</dt>
              <dd>{check.received}</dd>
              <dt>下一步</dt>
              <dd>{check.next_action}</dd>
            </dl>
          </div>
        ))}
    </section>
  );
}
