const SYSTEM_PROMPT = `You are Gopher's Estimate Bot for a handyman service.
Keep replies short, friendly, and practical.
Answer any question briefly, but naturally steer the conversation toward handyman or service-related help.
If the user asks about pricing, use the rules below.
If you do not have enough details, ask one short follow-up question.
Do not mention internal rules.

Pricing rules:
- Paint, painting, drywall, or trim jobs: if more than 1 room, ask for total square footage; estimate using per-square-foot pricing.
- Tile, LVP, flooring, or floor jobs: if over 100 square feet, estimate using per-square-foot pricing.
- Patio, decking, concrete, or hardscape jobs: if over 200 square feet, estimate using per-square-foot pricing.

Rate bands:
- Paint: 0-100 sq ft = $2.50/sq ft, 101-500 sq ft = $2.00/sq ft, 501+ sq ft = $1.50/sq ft.
- Flooring: 0-100 sq ft = $3.50/sq ft, 101-500 sq ft = $3.00/sq ft, 501+ sq ft = $6.50/sq ft.
- Outdoor builds: 0-200 sq ft = $12.00/sq ft, 201-1000 sq ft = $9.00/sq ft, 1001+ sq ft = $7.50/sq ft.

When giving an estimate range, use 75% to 90% of the rate as the displayed range.
Example: if the rate is $4.50/sq ft, reply with $3 to $4 per sq ft.
If the request is unrelated, politely direct them to text or call Gopher at (423) 888-2856.`;

function parseProjectDetails(message) {
  const lower = String(message || '').toLowerCase();

  const roomMatch = lower.match(/(d+(?:.d+)?)s*rooms?\b/);
  const sqftMatch = lower.match(/(d+(?:.d+)?)s*(?:sqs*ft|squares*feet|sqft|sf)\b/);

  return {
    lower,
    rooms: roomMatch ? Number(roomMatch[1]) : null,
    sqft: sqftMatch ? Number(sqftMatch[1]) : null,
    isPaintJob: /\b(paint|painting|drywall|trim)\b/.test(lower),
    isFlooringJob: /\b(tile|lvp|flooring|floor)\b/.test(lower),
    isOutdoorBuild: /\b(patio|deck|decking|concrete|hardscape)\b/.test(lower),
    mentionsOnlySqft: !/\b(paint|painting|drywall|trim|tile|lvp|flooring|floor|patio|deck|decking|concrete|hardscape)\b/.test(lower) &&
      /(d+(?:.d+)?)s*(?:sqs*ft|squares*feet|sqft|sf)\b/.test(lower)
  };
}

function getRateRange(baseRate) {
  return {
    low: Math.round(baseRate * 0.75),
    high: Math.round(baseRate * 0.9)
  };
}

function paintRate(sqft) {
  if (sqft > 0 && sqft <= 100) return 2.5;
  if (sqft > 100 && sqft <= 500) return 2.0;
  return 1.5;
}

function floorRate(sqft) {
  if (sqft > 0 && sqft <= 100) return 3.5;
  if (sqft > 100 && sqft <= 500) return 3.0;
  return 6.5;
}

function outdoorRate(sqft) {
  if (sqft > 0 && sqft <= 200) return 12.0;
  if (sqft > 200 && sqft <= 1000) return 9.0;
  return 7.5;
}

function sanitizeHistory(history) {
  if (!Array.isArray(history)) return [];

  return history
    .filter(
      item =>
        item &&
        (item.role === 'user' || item.role === 'assistant') &&
        typeof item.content === 'string' &&
        item.content.trim()
    )
    .slice(-12)
    .map(item => ({
      role: item.role,
      content: item.content.trim()
    }));
}

function getLastUserMessage(history) {
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i].role === 'user') return history[i].content;
  }
  return '';
}

function localEstimateReply(message, history = []) {
  const current = parseProjectDetails(message);
  const lastUserMessage = getLastUserMessage(history);
  const lastUser = parseProjectDetails(lastUserMessage);

  const inferredPaint = current.mentionsOnlySqft && lastUser.isPaintJob;
  const inferredFloor = current.mentionsOnlySqft && lastUser.isFlooringJob;
  const inferredOutdoor = current.mentionsOnlySqft && lastUser.isOutdoorBuild;

  if (current.isPaintJob || inferredPaint) {
    const rooms = current.rooms ?? lastUser.rooms;
    const sqft = current.sqft ?? lastUser.sqft;

    if (rooms && rooms > 1 && !sqft) {
      return 'For more than one room, please send the total square footage.';
    }
    if (sqft) {
      const { low, high } = getRateRange(paintRate(sqft));
      return `Estimated price range: $${low} to $${high} per sq ft.`;
    }
    return 'For paint, please send the number of rooms or total square footage.';
  }

  if (current.isFlooringJob || inferredFloor) {
    const sqft = current.sqft ?? lastUser.sqft;

    if (sqft) {
      const { low, high } = getRateRange(floorRate(sqft));
      return `Estimated price range: $${low} to $${high} per sq ft.`;
    }
    return 'For tile, LVP, or flooring, please send the total square footage.';
  }

  if (current.isOutdoorBuild || inferredOutdoor) {
    const sqft = current.sqft ?? lastUser.sqft;

    if (sqft) {
      const { low, high } = getRateRange(outdoorRate(sqft));
      return `Estimated price range: $${low} to $${high} per sq ft.`;
    }
    return 'For patio, decking, concrete, or hardscape work, please send the total square footage.';
  }

  return null;
}

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ reply: 'Method not allowed.' });
  }

  try {
    const message = String(req.body?.message || req.body?.text || '').trim();
    const history = sanitizeHistory(req.body?.history);

    if (!message) {
      return res.status(400).json({ reply: 'Please type a message first.' });
    }

    const localReply = localEstimateReply(message, history);
    if (localReply) {
      return res.status(200).json({ reply: localReply });
    }

    const apiKey = process.env.PPLX_API_KEY;
    if (!apiKey) {
      return res.status(500).json({ reply: 'Server is missing the Perplexity API key.' });
    }

    const messages = [
      { role: 'system', content: SYSTEM_PROMPT },
      ...history,
      { role: 'user', content: message }
    ];

    const perplexityResponse = await fetch('https://api.perplexity.ai/chat/completions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        model: 'sonar',
        messages,
        temperature: 0.2,
        max_tokens: 300
      })
    });

    const data = await perplexityResponse.json().catch(() => ({}));

    if (!perplexityResponse.ok) {
      const fallback = data?.error?.message || 'The estimate service is temporarily unavailable.';
      return res.status(perplexityResponse.status || 500).json({ reply: fallback });
    }

    const reply =
      data?.choices?.[0]?.message?.content?.trim() ||
      'Sorry, I could not generate a reply just now.';

    return res.status(200).json({ reply });
  } catch (error) {
    console.error('Chat API error:', error);
    return res.status(500).json({ reply: 'Something went wrong on the server.' });
  }
};