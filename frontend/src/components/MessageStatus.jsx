import React, { useState, useEffect } from 'react';

const MessageStatus = () => {
  const [messages, setMessages] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchMessages();
    const interval = setInterval(fetchMessages, 30000); // Update every 30s
    return () => clearInterval(interval);
  }, []);

  const fetchMessages = async () => {
    try {
      const [statusRes, summaryRes] = await Promise.all([
        fetch('/api/messages/status?limit=50'),
        fetch('/api/messages/status/summary')
      ]);
      
      const statusData = await statusRes.json();
      const summaryData = await summaryRes.json();
      
      setMessages(statusData.messages || []);
      setSummary(summaryData);
    } catch (error) {
      console.error('Error fetching messages:', error);
    } finally {
      setLoading(false);
    }
  };

  if (loading) return <div>Loading...</div>;

  return (
    <div style={{ padding: '20px' }}>
      <h2>📱 Telegram Message Tracking</h2>
      
      {/* Summary Stats */}
      {summary && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '10px', marginBottom: '20px' }}>
          <div style={{ background: '#4CAF50', color: 'white', padding: '15px', borderRadius: '8px' }}>
            <div style={{ fontSize: '24px', fontWeight: 'bold' }}>{summary.REPLIED}</div>
            <div>✅ Replied</div>
          </div>
          <div style={{ background: '#2196F3', color: 'white', padding: '15px', borderRadius: '8px' }}>
            <div style={{ fontSize: '24px', fontWeight: 'bold' }}>{summary.VIEWED}</div>
            <div>👁️ Viewed</div>
          </div>
          <div style={{ background: '#FF9800', color: 'white', padding: '15px', borderRadius: '8px' }}>
            <div style={{ fontSize: '24px', fontWeight: 'bold' }}>{summary.EXPIRED}</div>
            <div>⏰ Expired</div>
          </div>
          <div style={{ background: '#9C27B0', color: 'white', padding: '15px', borderRadius: '8px' }}>
            <div style={{ fontSize: '24px', fontWeight: 'bold' }}>{summary.SENT}</div>
            <div>📤 Sent</div>
          </div>
        </div>
      )}
      
      {/* Messages Table */}
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ background: '#f5f5f5' }}>
            <th style={{ textAlign: 'left', padding: '10px', borderBottom: '2px solid #ddd' }}>Phone</th>
            <th style={{ textAlign: 'left', padding: '10px', borderBottom: '2px solid #ddd' }}>Message</th>
            <th style={{ textAlign: 'left', padding: '10px', borderBottom: '2px solid #ddd' }}>Sent</th>
            <th style={{ textAlign: 'left', padding: '10px', borderBottom: '2px solid #ddd' }}>Status</th>
          </tr>
        </thead>
        <tbody>
          {messages.map((msg) => (
            <tr key={msg.id} style={{ borderBottom: '1px solid #ddd' }}>
              <td style={{ padding: '10px' }}><strong>{msg.phone}</strong></td>
              <td style={{ padding: '10px' }}>{msg.text.substring(0, 50)}...</td>
              <td style={{ padding: '10px', fontSize: '12px' }}>
                {new Date(msg.sent_at).toLocaleString()}
              </td>
              <td style={{ padding: '10px' }}>
                <StatusBadge status={msg.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

const StatusBadge = ({ status }) => {
  const styles = {
    'REPLIED': { background: '#4CAF50', color: 'white' },
    'VIEWED': { background: '#2196F3', color: 'white' },
    'EXPIRED': { background: '#FF9800', color: 'white' },
    'SENT': { background: '#9C27B0', color: 'white' },
  };
  
  const icons = {
    'REPLIED': '✅',
    'VIEWED': '👁️',
    'EXPIRED': '⏰',
    'SENT': '📤',
  };
  
  const style = styles[status.split(' ')[0]] || { background: '#999', color: 'white' };
  const icon = icons[status.split(' ')[0]] || '❓';
  
  return (
    <span style={{
      ...style,
      padding: '5px 10px',
      borderRadius: '4px',
      fontSize: '12px',
      fontWeight: 'bold'
    }}>
      {icon} {status}
    </span>
  );
};

export default MessageStatus;
