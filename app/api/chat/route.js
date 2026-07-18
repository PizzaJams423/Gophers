export async function POST(req) {
  try {
    const body = await req.json();
    const message = String(body.message || body.text || '').trim();
    const systemPrompt = String(body.systemPrompt || '').trim();
    const history = Array.isArray(body.history) ? body.history : [];

    if (!message) {
      return Response.json({ reply: 'Please type a message first.' }, { status: 400 });
    }

    const lower = message.toLowerCase();
    const isPaintJob = /\bpaint|painting|drywall|trim\b/.test(lower);
    const isFlooringJob = /\b(tile|lvp|flooring|floor)\b/.test(lower);
    const isOutdoorBuild = /\b(patio|deck|decking|concrete|hardscape)\b/.test(lower);

    const roomMatch = lower.match(/(d+(?:.d+)?)s*room/);
    const sqftMatch = lower.match(/(d+(?:.d+)?)s*(?:sqs*ft|squares*feet|sqft|sf)\b/);
    const sqft = sqftMatch ? Number(sqftMatch[1]) : null;

    const parseRate = (base) => ({ low: Math.round(base * 0.75), high: Math.round(base * 0.9) });
    const rateForPaint = (sqftVal) => {
      if (sqftVal > 0 && sqftVal <= 100) return 4.5;
      if (sqftVal > 100 && sqftVal <= 500) return 3.75;
      return 3.25;
    };
    const rateForFloor = (sqftVal) => {
      if (sqftVal > 0 && sqftVal <= 100) return 9.5;
      if (sqftVal > 100 && sqftVal <= 500) return 7.5;
      return 6.5;
    };
    const rateForOutdoor = (sqftVal) => {
      if (sqftVal > 0 && sqftVal <= 200) return 12;
      if (sqftVal > 200 && sqftVal <= 1000) return 9;
      return 7.5;
    };

    if (isPaintJob) {
      if (roomMatch && Number(roomMatch[1]) > 1) {
        if (!sqft) return Response.json({ reply: 'For more than one room, please send the total square footage.' });
        const rate = rateForPaint(sqft);
        const { low, high } = parseRate(rate);
        return Response.json({ reply: `Estimated price range: $${low} to $${high} per sq ft.` });
      }
      if (sqft) {
        const rate = rateForPaint(sqft);
        const { low, high } = parseRate(rate);
        return Response.json({ reply: `Estimated price range: $${low} to $${high} per sq ft.` });
      }
      return Response.json({ reply: 'For paint, send the number of rooms or total square footage.' });
    }

    if (isFlooringJob) {
      if (sqft && sqft > 100) {
        const rate = rateForFloor(sqft);
        const { low, high } = parseRate(rate);
        return Response.json({ reply: `Estimated price range: $${low} to $${high} per sq ft.` });
      }
      return Response.json({ reply: 'For tile, LVP, or flooring jobs over 100 square feet, send the total square footage.' });
    }

    if (isOutdoorBuild) {
      if (sqft && sqft > 200) {
        const rate = rateForOutdoor(sqft);
        const { low, high } = parseRate(rate);
        return Response.json({ reply: `Estimated price range: $${low} to $${high} per sq ft.` });
      }
      return Response.json({ reply: 'For patio, decking, concrete, or hardscape jobs over 200 square feet, send the total square footage.' });
    }

    if (/^(yes|yeah|yep|ok|okay|sure|sounds good|that works)\b/i.test(lower) && history.length) {
      const last = [...history].reverse().find(m => m && m.role === 'assistant' && m.content);
      if (last) return Response.json({ reply: last.content });
    }

    return Response.json({ reply: 'Send the job type and size, and I will estimate it using your pricing rules.' });
  } catch (error) {
    return Response.json({ reply: 'Something went wrong on the server.' }, { status: 500 });
  }
}
