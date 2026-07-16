export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
    const { message, systemPrompt, history = [] } = req.body || {};
    const apiKey = process.env.PPLX_API_KEY;

    if (!apiKey) {
      return res.status(500).json({ error: 'Missing API key' });
    }

    const messages = [
      { role: 'system', content: systemPrompt || 'You are a helpful assistant.' },
      ...history.filter(m => m && m.role && m.content),
      { role: 'user', content: message }
    ];

    const response = await fetch('https://api.perplexity.ai/chat/completions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        model: 'sonar',
        messages
      })
    });

    if (!response.ok) {
      const errorText = await response.text();
      return res.status(500).json({ error: 'Upstream API error', details: errorText });
    }

    const data = await response.json();
    const reply = data?.choices?.[0]?.message?.content || 'Sorry, I could not generate a reply just now.';
    return res.status(200).json({ reply });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', details: String(err) });
  }
}
