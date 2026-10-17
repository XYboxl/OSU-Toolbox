// Minimal DOM shim so the real app.js can be driven in node.
class Element {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.childNodes = this.children;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.listeners = new Map();
    this._text = '';
    this._value = '';
    this.checked = false;
    this.hidden = false;
    this.disabled = false;
    this.href = '';
    this.type = '';
    this.className = '';
    this.title = '';
    this.name = '';
    this.htmlFor = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.classList = {
      add: (name) => {
        const set = new Set(String(this.className || '').split(/\s+/).filter(Boolean));
        set.add(name);
        this.className = [...set].join(' ');
      },
      remove: (name) => {
        const set = new Set(String(this.className || '').split(/\s+/).filter(Boolean));
        set.delete(name);
        this.className = [...set].join(' ');
      },
      contains: (name) =>
        String(this.className || '').split(/\s+/).filter(Boolean).includes(name),
    };
  }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((child) => child.textContent).join('');
  }

  set textContent(value) {
    this._text = value === null || value === undefined ? '' : String(value);
    const unregister = (node) => {
      if (globalThis.document?.byId.get(node.id) === node) globalThis.document.byId.delete(node.id);
      for (const child of node.children) unregister(child);
    };
    for (const child of this.children) unregister(child);
    this.children.length = 0;
  }

  get value() {
    return this._value;
  }

  set value(next) {
    this._value = next === null || next === undefined ? '' : String(next);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'id') this.id = String(value);
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    // A real document indexes every element by id as soon as it is inserted.
    if (globalThis.document && child.id) globalThis.document.byId.set(child.id, child);
    if (globalThis.document) {
      const walk = (node) => {
        if (node.id) globalThis.document.byId.set(node.id, node);
        for (const grand of node.children || []) walk(grand);
      };
      walk(child);
    }
    return child;
  }

  append(...nodes) {
    for (const node of nodes) this.appendChild(node);
  }

  remove() {
    if (this.parentNode) {
      const index = this.parentNode.children.indexOf(this);
      if (index >= 0) this.parentNode.children.splice(index, 1);
    }
  }

  focus() {}

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  removeEventListener(type, handler) {
    const list = this.listeners.get(type) || [];
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }

  dispatch(type, event = {}) {
    const list = this.listeners.get(type) || [];
    const payload = { type, target: this, preventDefault() {}, ...event };
    return Promise.all(list.map((handler) => handler(payload)));
  }

  get childElementCount() {
    return this.children.length;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const matches = (node, part) => {
      if (part.startsWith('.')) return node.classList.contains(part.slice(1));
      const attribute = part.match(/^\[([^=]+)="([^"]+)"\]$/);
      if (attribute) {
        const key = attribute[1].replace(/^data-/, '').replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
        return node.dataset[key] === attribute[2] || node.getAttribute(attribute[1]) === attribute[2];
      }
      return node.tagName.toLowerCase() === part.toLowerCase();
    };
    const found = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (selector.split(',').some(part => matches(child, part.trim()))) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
}

class Document {
  constructor() {
    this.byId = new Map();
    this.body = new Element('body');
  }

  register(id) {
    if (!this.byId.has(id)) {
      const element = new Element('div');
      element.id = id;
      this.byId.set(id, element);
    }
    return this.byId.get(id);
  }

  createElement(tag) {
    return new Element(tag);
  }

  getElementById(id) {
    return this.byId.get(id) || null;
  }
}

function installGlobals() {
  const document = new Document();
  const window = {
    listeners: new Map(),
    location: { hash: '' },
    pywebview: { api: {} },
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    addEventListener(type, handler) {
      if (!window.listeners.has(type)) window.listeners.set(type, []);
      window.listeners.get(type).push(handler);
    },
    dispatch(type, event = {}) {
      const list = window.listeners.get(type) || [];
      return Promise.all(list.map((handler) => handler({ type, ...event })));
    },
  };
  globalThis.document = document;
  globalThis.window = window;
  return { document, window };
}

module.exports = { Element, Document, installGlobals };
