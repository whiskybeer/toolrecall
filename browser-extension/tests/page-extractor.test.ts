import { describe, it, expect, beforeEach } from 'vitest';
import { extractPageContent } from '../src/page-extractor';

describe('extractPageContent', () => {
  beforeEach(() => {
    // Reset the document for each test
    document.title = '';
    document.body.innerHTML = '';
  });

  it('extracts title and HTML from page', () => {
    document.title = 'Test Page';
    document.body.innerHTML = '<p>Hello World</p><a href="https://example.com">Link</a>';

    const result = extractPageContent();
    expect(result.title).toBe('Test Page');
    expect(result.html).toContain('Hello World');
    expect(result.url).toBeTruthy();
  });

  it('extracts text content from body', () => {
    document.title = 'Text Test';
    document.body.innerHTML = '<p>Hello World</p><div>Second paragraph</div>';

    const result = extractPageContent();
    expect(result.text).toContain('Hello World');
    expect(result.text).toContain('Second paragraph');
  });

  it('builds snapshot with interactive elements', () => {
    document.title = 'Interactive Page';
    document.body.innerHTML = `
      <a href="https://example.com">Click me</a>
      <button id="submit">Submit</button>
      <input type="text" placeholder="Search">
      <details><summary>Details</summary>Content</details>
    `;

    const result = extractPageContent();
    expect(result.snapshot).toContain('Interactive Page');
    expect(result.snapshot).toContain('Interactive Elements');
    expect(result.snapshot).toContain('Submit');
  });

  it('generates consistent hash for same content', () => {
    document.title = 'Same';
    document.body.innerHTML = '<p>Same content</p>';

    const result1 = extractPageContent();
    const result2 = extractPageContent();
    expect(result1.contentHash).toBe(result2.contentHash);
  });

  it('generates different hash for different content', () => {
    document.title = 'First';
    document.body.innerHTML = '<p>Version A</p>';
    const result1 = extractPageContent();

    document.title = 'Second';
    document.body.innerHTML = '<p>Version B</p>';
    const result2 = extractPageContent();

    expect(result1.contentHash).not.toBe(result2.contentHash);
  });

  it('handles empty page gracefully', () => {
    document.title = '';
    document.body.innerHTML = '';

    const result = extractPageContent();
    expect(result.title).toBe('(no title)');
    expect(result.text).toBe('');
    expect(result.contentHash).toBeDefined();
  });

  it('includes URLs in snapshot for anchor elements', () => {
    document.title = 'Links';
    document.body.innerHTML = '<a href="https://example.com/page">Example Link</a>';

    const result = extractPageContent();
    expect(result.snapshot).toContain('→');
    expect(result.snapshot).toContain('https://example.com/page');
  });

  it('limits text to MAX_TEXT_LENGTH', () => {
    document.title = 'Long Page';
    // Create 200K chars of text
    document.body.innerHTML = '<p>' + 'x'.repeat(200000) + '</p>';

    const result = extractPageContent();
    // text should be capped at 100K
    expect(result.text.length).toBe(100000);
  });
});
