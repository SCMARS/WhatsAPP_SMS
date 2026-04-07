import { useEffect, useMemo, useState } from "react";
import Papa from "papaparse";
import { BulkPayload, Lead, StopPayload } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8001";
const API_KEY_FROM_ENV =
  import.meta.env.VITE_API_SECRET_KEY ||
  // backward-compat (old name)
  import.meta.env.VITE_API_KEY ||
  "";
const AGENT_ID_FROM_ENV = import.meta.env.VITE_AGENT_ID || "";

type CsvRow = Record<string, string>;

function formatApiError(detail: unknown): string {
  if (!detail) return "Ошибка запроса";
  if (typeof detail === "string") return detail;
  try {
    return JSON.stringify(detail);
  } catch {
    return String(detail);
  }
}

function parseCsv(file: File, onParsed: (rows: Lead[]) => void, onError: (msg: string) => void) {
  Papa.parse<CsvRow>(file, {
    header: true,
    skipEmptyLines: true,
    complete: (results) => {
      if (results.errors.length) {
        onError(`CSV error: ${results.errors[0].message}`);
      }
      const rows: Lead[] = results.data
        .map((row, idx) => {
          const phone =
            (row.phone || row.phone_number || row.whatsapp || "").trim();
          if (!phone) return null;
          const lead_id = (row.lead_id || row.id || phone || `lead-${idx + 1}`).trim();
          const lead_name = (row.lead_name || row.name || "").trim() || undefined;
          const initial_message = (row.initial_message || row.message || "").trim() || undefined;
          const campaign_external_id = (row.campaign_external_id || row.campaign || "").trim() || undefined;
          return { phone, lead_id, lead_name, initial_message, campaign_external_id };
        })
        .filter(Boolean) as Lead[];
      onParsed(rows);
    },
    error: (err) => onError(err.message),
  });
}

function formatCount(n: number) {
  return new Intl.NumberFormat("ru-RU").format(n);
}

export default function App() {
  const [apiKey, setApiKey] = useState(API_KEY_FROM_ENV);
  const [agentId, setAgentId] = useState(AGENT_ID_FROM_ENV);
  const campaignId = "default";
  const [leads, setLeads] = useState<Lead[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const totalLeads = useMemo(() => leads.length, [leads]);

  useEffect(() => {
    // If backend exposes config, grab key/agent automatically.
    fetch(`${API_BASE}/api/config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.api_key && !apiKey) setApiKey(data.api_key);
        if (data?.agent_id && !agentId) setAgentId(data.agent_id);
      })
      .catch(() => {});
  }, [apiKey, agentId]);

  const handleSaveAgent = async () => {
    setStatus(null);
    setError(null);
    if (!apiKey) {
      setError("Ключ не настроен.");
      return;
    }
    if (!agentId.trim()) {
      setError("Укажите Agent ID.");
      return;
    }
    try {
      setLoading(true);
      const res = await fetch(`${API_BASE}/api/config/agent`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": apiKey,
        },
        body: JSON.stringify({ agent_id: agentId.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(formatApiError(data?.detail ?? data));
      setStatus(`Agent ID сохранен: ${data.agent_id}`);
    } catch (err: any) {
      setError(err.message || "Ошибка сохранения Agent ID");
    } finally {
      setLoading(false);
    }
  };

  const handleFile = (file?: File) => {
    if (!file) return;
    setError(null);
    parseCsv(
      file,
      (rows) => setLeads(rows),
      (msg) => setError(msg),
    );
  };

  const handleSend = async () => {
    setStatus(null);
    setError(null);
    if (!apiKey) {
      setError("Ключ не настроен. Проверьте backend `/api/config` или задайте VITE_API_SECRET_KEY.");
      return;
    }
    if (!totalLeads) {
      setError("Нет лидов для отправки.");
      return;
    }
    const payload: BulkPayload = {
      campaign_external_id: campaignId,
      leads: leads.map((lead, idx) => ({
        ...lead,
        campaign_external_id: lead.campaign_external_id || campaignId,
        initial_message: (() => {
          const msg = (lead.initial_message ?? "").trim();
          return msg.length ? msg : undefined; 
        })(),
        batch_index: idx,
      })),
    };
    try {
      setLoading(true);
      const res = await fetch(`${API_BASE}/api/send/bulk`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": apiKey,
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(formatApiError(data?.detail ?? data));
      }
      const sent = data.sent ?? 0;
      const total = data.total ?? totalLeads;
      if (sent > 0) {
        setStatus(`Отправили: ${sent} из ${total}`);
      } else {
        const firstBad = Array.isArray(data.results)
          ? data.results.find((r: any) => r.status !== "sent")
          : null;
        const detail = firstBad?.detail ? ` Причина: ${firstBad.detail}` : "";
        setStatus(`Отправили: 0 из ${total}.${detail}`);
      }
    } catch (err: any) {
      setError(err.message || "Ошибка отправки");
    } finally {
      setLoading(false);
    }
  };

  const handleStopAll = async () => {
    setStatus(null);
    setError(null);
    if (!apiKey) {
      setError("Ключ не настроен. Проверьте backend `/api/config` или задайте VITE_API_SECRET_KEY.");
      return;
    }
    if (!totalLeads) {
      setError("Нет лидов для остановки.");
      return;
    }
    const payload: StopPayload = { phones: leads.map((l) => l.phone) };
    try {
      setLoading(true);
      const res = await fetch(`${API_BASE}/api/stop/bulk`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": apiKey,
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(formatApiError(data?.detail ?? data));
      setStatus(`Остановлено: ${data.stopped ?? data.total ?? totalLeads} из ${data.total ?? totalLeads}`);
    } catch (err: any) {
      setError(err.message || "Ошибка остановки");
    } finally {
      setLoading(false);
    }
  };

  const handleStopOne = async (phone: string) => {
    setError(null);
    if (!apiKey) {
      setError("Ключ не настроен. Проверьте backend `/api/config` или задайте VITE_API_SECRET_KEY.");
      return;
    }
    try {
      setLoading(true);
      const res = await fetch(`${API_BASE}/api/stop`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": apiKey,
        },
        body: JSON.stringify({ phone }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(formatApiError(data?.detail ?? data));
      setStatus(`Стоп для ${phone}: ${data.status}`);
    } catch (err: any) {
      setError(err.message || "Ошибка стопа");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="app">
      <h1>WR WhatsApp Dashboard</h1>
      <div className="card">
        <div className="row" style={{ alignItems: "center", gap: 16 }}>
          <div style={{ flex: "1 1 240px" }}>
            <label>x-api-key (API_SECRET_KEY)</label>
            <div style={{ fontSize: 13, color: apiKey ? "#16a34a" : "#dc2626" }}>
              {apiKey ? "Ключ настроен автоматически" : "Ключ не найден (проверяем /api/config)"}
            </div>
          </div>
          <div style={{ flex: "1 1 320px" }}>
            <label>Agent ID (ElevenLabs)</label>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                type="text"
                placeholder="agent_xxx"
                value={agentId}
                onChange={(e) => setAgentId(e.target.value)}
                disabled={loading}
              />
              <button className="secondary" onClick={handleSaveAgent} disabled={loading}>
                Сохранить
              </button>
            </div>
          </div>
          <div style={{ flex: "1 1 320px" }}>
            <label>Загрузить CSV (столбцы: phone, lead_id, lead_name, initial_message, campaign_external_id)</label>
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => handleFile(e.target.files?.[0])}
            />
          </div>
          <div className="pill">
            Лидов: {formatCount(totalLeads)}
          </div>
          <button className="secondary" onClick={() => setLeads([])}>
            Очистить
          </button>
          <button onClick={handleSend} disabled={loading || !totalLeads}>
            {loading ? "..." : "Старт всем"}
          </button>
          <button onClick={handleStopAll} disabled={loading || !totalLeads}>
            {loading ? "..." : "Стоп всем"}
          </button>
        </div>
      </div>

      {error && <div className="card" style={{ color: "#dc2626" }}>{error}</div>}
      {status && <div className="card status-ok">{status}</div>}

      {leads.length > 0 && (
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h3>Предпросмотр ({formatCount(Math.min(10, leads.length))} из {formatCount(leads.length)})</h3>
            <span style={{ color: "#6b7280", fontSize: 13 }}>Показываем первые 10 строк</span>
          </div>
          <table className="leads-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Телефон</th>
                <th>Lead ID</th>
                <th>Имя</th>
                <th>Сообщение</th>
                <th>Кампания</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {leads.slice(0, 10).map((lead, idx) => (
                <tr key={idx}>
                  <td>{idx + 1}</td>
                  <td>{lead.phone}</td>
                  <td>{lead.lead_id}</td>
                  <td>{lead.lead_name || "—"}</td>
                  <td style={{ maxWidth: 280, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {lead.initial_message || "first_message из ElevenLabs"}
                  </td>
                  <td>{lead.campaign_external_id || campaignId}</td>
                  <td>
                    <button
                      className="secondary"
                      onClick={() => handleStopOne(lead.phone)}
                      disabled={loading}
                    >
                      Стоп
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
