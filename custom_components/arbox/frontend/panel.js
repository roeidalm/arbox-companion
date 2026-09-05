import {feedbackTemplate, mountFeedback} from './feedback-form.js?v=2.9.0';

/** HA provides the authenticated websocket; no Arbox URL or API key enters the browser. */
class ArboxFeedbackPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: 'open'});
    this._onLocationChange = () => this._render();
  }

  set hass(value) {
    this._hass = value;
    this.style.colorScheme = value.themes?.darkMode ? 'dark' : 'light';
    if (this._menu) this._menu.hass = value;
    this._render();
  }
  set panel(value) { this._panel = value; this._render(); }
  set narrow(value) { if (this._menu) this._menu.narrow = value; this._narrow = value; }

  connectedCallback() {
    window.addEventListener('hashchange', this._onLocationChange);
    window.addEventListener('location-changed', this._onLocationChange);
    this._render();
  }
  disconnectedCallback() {
    window.removeEventListener('hashchange', this._onLocationChange);
    window.removeEventListener('location-changed', this._onLocationChange);
    this._renderKey = undefined;
  }

  _render() {
    if (!this.isConnected || !this._hass || !this._panel) return;
    const token = window.location.hash.slice(1);
    const configuredEntry = this._panel.config?.entry_id;
    const key = `${configuredEntry || ''}:${token}`;
    if (key === this._renderKey) return;
    this._renderKey = key;

    const stylesheet = document.createElement('link');
    stylesheet.rel = 'stylesheet';
    stylesheet.href = new URL('./feedback.css?v=2.9.0', import.meta.url).href;
    const toolbar = document.createElement('div');
    toolbar.style.cssText = 'display:flex;align-items:center;gap:12px;min-height:56px;padding:0 12px;background:var(--card);border-bottom:1px solid var(--line);font:18px system-ui';
    toolbar.dir = 'rtl';
    this._menu = document.createElement('ha-menu-button');
    this._menu.hass = this._hass;
    this._menu.narrow = this._narrow;
    const title = document.createElement('span');
    title.textContent = 'משוב על האימון';
    toolbar.append(this._menu, title);
    const content = document.createElement('div');
    content.className = 'feedback-shell';
    content.dir = 'rtl';
    content.lang = 'he';
    content.innerHTML = feedbackTemplate;
    this.shadowRoot.replaceChildren(stylesheet, toolbar, content);

    let entryId = configuredEntry;
    const request = async (method, feedback) => {
      if (!token) throw new Error('פתחו את הקישור בהתראת האימון כדי למלא משוב. הקישור מזהה את האימון המתאים.');
      const message = {type: method === 'GET' ? 'arbox/feedback/read' : 'arbox/feedback/save', token};
      if (entryId) message.entry_id = entryId;
      if (method !== 'GET') message.feedback = feedback;
      try {
        const result = await this._hass.callWS(message);
        if (result.entry_id) entryId = result.entry_id;
        return result;
      } catch (error) {
        throw new Error(error.message || 'לא ניתן להתחבר לשרת Arbox דרך Home Assistant. נסו שוב.');
      }
    };
    mountFeedback(content, request);
  }
}

if (!customElements.get('arbox-feedback-panel')) {
  customElements.define('arbox-feedback-panel', ArboxFeedbackPanel);
}
