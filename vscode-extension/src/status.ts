/* ------------------------------------------------------------------
 * ToolRecall VS Code Extension — StatusBar Component
 *
 * Shows cache hit/miss counter in the VS Code status bar.
 * ------------------------------------------------------------------ */

import * as vscode from 'vscode';

const STATUS_ITEM_ID = 'toolrecall.statusBar';
const STATUS_PRIORITY = 100;

export class StatusBarManager {
  private item: vscode.StatusBarItem;
  private _hits = 0;
  private _misses = 0;
  private _connected = false;

  constructor() {
    this.item = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Right,
      STATUS_PRIORITY
    );
    this.item.name = 'ToolRecall Cache';
    this.item.command = 'toolrecall.showStatus';
    this.item.tooltip = 'ToolRecall Cache — Click for details';
    this.updateDisplay();
    this.item.show();
  }

  get hits(): number { return this._hits; }
  get misses(): number { return this._misses; }

  /** Set connection status */
  set connected(value: boolean) {
    this._connected = value;
    this.updateDisplay();
  }

  /** Record a cache hit */
  recordHit(): void {
    this._hits++;
    this.updateDisplay();
  }

  /** Record a cache miss */
  recordMiss(): void {
    this._misses++;
    this.updateDisplay();
  }

  /** Reset counters (on workspace change) */
  reset(): void {
    this._hits = 0;
    this._misses = 0;
    this._connected = false;
    this.updateDisplay();
  }

  /** Update the status bar text */
  private updateDisplay(): void {
    if (!this._connected) {
      this.item.text = '$(database) TR: --';
      this.item.backgroundColor = undefined;
      return;
    }

    this.item.text = `$(database) TR: ${this._hits}H / ${this._misses}M`;
    this.item.backgroundColor = undefined;
  }

  /** Show a detailed status message */
  async showDetails(): Promise<void> {
    const message = [
      `**ToolRecall Cache**`,
      ``,
      `Hits:  ${this._hits}`,
      `Misses: ${this._misses}`,
      `Total: ${this._hits + this._misses}`,
      `Hit rate: ${this._hits + this._misses > 0
          ? ((this._hits / (this._hits + this._misses)) * 100).toFixed(1)
          : '—'}%`,
      ``,
      `Connected: ${this._connected ? '✓' : '✗'}`,
    ].join('\n');

    const selection = await vscode.window.showInformationMessage(
      message,
      { modal: false },
      'Reset Counters'
    );

    if (selection === 'Reset Counters') {
      this.reset();
    }
  }

  /** Dispose the status bar item */
  dispose(): void {
    this.item.dispose();
  }
}
