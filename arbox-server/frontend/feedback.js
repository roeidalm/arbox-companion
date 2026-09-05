import {feedbackTemplate, mountFeedback} from './feedback-form.js';

// Fragment capabilities stay out of HTTP access logs and referrers.
const token = location.hash.slice(1);
document.body.innerHTML = feedbackTemplate;
  async function request(method, body) {
    const response = await fetch('/api/feedback', {method, cache: 'no-store', headers: {'X-Feedback-Token': token, 'Content-Type': 'application/json'}, ...(body ? {body: JSON.stringify(body)} : {})});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'יש לבדוק את הערכים בטופס ולנסות שוב.');
    return result;
  }
mountFeedback(document, request);
