'use client';

import React, { useState, useEffect, useRef } from 'react';

// API & WS config
const getApiBase = () => {
  if (typeof window === 'undefined') return 'http://localhost:8765';
  const port = window.location.port ? `:${window.location.port}` : '';
  if (window.location.port === '3000') {
    return `${window.location.protocol}//${window.location.hostname}:8765`;
  }
  return `${window.location.protocol}//${window.location.hostname}${port}`;
};

const getWsBase = () => {
  if (typeof window === 'undefined') return 'ws://localhost:8765';
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  if (window.location.port === '3000') {
    return `${proto}//${window.location.hostname}:8765`;
  }
  const port = window.location.port ? `:${window.location.port}` : '';
  return `${proto}//${window.location.hostname}${port}`;
};

export default function Home() {
  const [theme, setTheme] = useState<'light' | 'dark'>('light');
  const [currentView, setCurrentView] = useState<'runs' | 'benchmarks' | 'compare' | 'run-details'>('runs');
  
  // Data states
  const [runs, setRuns] = useState<any[]>([]);
  const [benchmarks, setBenchmarks] = useState<any[]>([]);
  const [selectedRun, setSelectedRun] = useState<any | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedBenchmark, setSelectedBenchmark] = useState<any | null>(null);
  const [runDetailTab, setRunDetailTab] = useState<'table' | 'console'>('table');
  
  // Create Run Form state
  const [runName, setRunName] = useState('');
  const [runBenchmarkId, setRunBenchmarkId] = useState('');
  const [runHarnessType, setRunHarnessType] = useState('cli');
  const [runModel, setRunModel] = useState('');
  const [runBaseUrl, setRunBaseUrl] = useState('');
  const [runCliCommand, setRunCliCommand] = useState('');
  const [runConcurrency, setRunConcurrency] = useState(5);
  const [runTimeout, setRunTimeout] = useState(600);
  const [runRecursionLimit, setRunRecursionLimit] = useState(100);
  const [runGlobalTokenBudget, setRunGlobalTokenBudget] = useState(-1);
  const [runEnvVarsText, setRunEnvVarsText] = useState('{\n  "GIGACHAT_CREDENTIALS": ""\n}');
  const [isCreatingRun, setIsCreatingRun] = useState(false);

  // Create Benchmark Form state
  const [bmName, setBmName] = useState('');
  const [bmDescription, setBmDescription] = useState('');
  const [isCreatingBenchmark, setIsCreatingBenchmark] = useState(false);

  // Group Form state
  const [groupName, setGroupName] = useState('');
  const [groupDescription, setGroupDescription] = useState('');
  const [groupDefaultBudget, setGroupDefaultBudget] = useState(-1);
  const [groupDefaultTimeout, setGroupDefaultTimeout] = useState(600);
  const [isCreatingGroup, setIsCreatingGroup] = useState(false);

  // Test Form state
  const [testName, setTestName] = useState('');
  const [testPrompt, setTestPrompt] = useState('');
  const [testTagsText, setTestTagsText] = useState('[]');
  const [testSetupFilesText, setTestSetupFilesText] = useState('{}');
  const [testGoldFilesText, setTestGoldFilesText] = useState('{}');
  const [testVerifierType, setTestVerifierType] = useState('');
  const [testTokenBudget, setTestTokenBudget] = useState(-1);
  const [testTimeout, setTestTimeout] = useState(600);
  const [selectedGroupId, setSelectedGroupId] = useState('');
  const [isCreatingTest, setIsCreatingTest] = useState(false);

  // Compare state
  const [compareId1, setCompareId1] = useState('');
  const [compareId2, setCompareId2] = useState('');
  const [compareResult, setCompareResult] = useState<any | null>(null);
  const [isComparing, setIsComparing] = useState(false);

  // Realtime WS
  const wsRef = useRef<WebSocket | null>(null);

  // Initialize theme and load data
  useEffect(() => {
    // Theme check
    const savedTheme = localStorage.getItem('theme') as 'light' | 'dark';
    if (savedTheme) {
      setTheme(savedTheme);
      document.documentElement.setAttribute('data-theme', savedTheme);
    } else {
      document.documentElement.setAttribute('data-theme', 'light');
    }
    
    fetchRuns();
    fetchBenchmarks();
  }, []);

  // Sync theme
  const toggleTheme = () => {
    const nextTheme = theme === 'light' ? 'dark' : 'light';
    setTheme(nextTheme);
    localStorage.setItem('theme', nextTheme);
    document.documentElement.setAttribute('data-theme', nextTheme);
  };

  // Poll active runs status
  useEffect(() => {
    const activeInterval = setInterval(() => {
      const hasActive = runs.some(r => r.status === 'running' || r.status === 'pending');
      if (hasActive) {
        fetchRuns();
      }
    }, 5000);
    return () => clearInterval(activeInterval);
  }, [runs]);

  // Fetch runs
  const fetchRuns = async () => {
    try {
      const res = await fetch(`${getApiBase()}/api/runs`);
      if (res.ok) {
        const data = await res.json();
        setRuns(data);
      }
    } catch (err) {
      console.error('Failed to fetch runs:', err);
    }
  };

  // Fetch benchmarks
  const fetchBenchmarks = async () => {
    try {
      const res = await fetch(`${getApiBase()}/api/benchmarks`);
      if (res.ok) {
        const data = await res.json();
        setBenchmarks(data);
        if (data.length > 0 && !runBenchmarkId) {
          setRunBenchmarkId(data[0].id);
        }
      }
    } catch (err) {
      console.error('Failed to fetch benchmarks:', err);
    }
  };

  // Fetch benchmark details
  const fetchBenchmarkDetails = async (id: string) => {
    try {
      const res = await fetch(`${getApiBase()}/api/benchmarks/${id}`);
      if (res.ok) {
        const data = await res.json();
        setSelectedBenchmark(data);
      }
    } catch (err) {
      console.error('Failed to fetch benchmark details:', err);
    }
  };

  // Fetch single run details
  const fetchRunDetails = async (id: string) => {
    try {
      const res = await fetch(`${getApiBase()}/api/runs/${id}`);
      if (res.ok) {
        const data = await res.json();
        setSelectedRun(data);
      }
    } catch (err) {
      console.error('Failed to fetch run details:', err);
    }
  };

  // WebSocket connection for real-time progress
  useEffect(() => {
    if (currentView === 'run-details' && selectedRunId) {
      // Connect WS
      const wsUrl = `${getWsBase()}/api/runs/${selectedRunId}/ws`;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'task_update' || msg.type === 'task_completed' || msg.type === 'run_completed') {
          // Refresh details
          fetchRunDetails(selectedRunId);
          fetchRuns();
        }
      };

      ws.onclose = () => {
        console.log('WS closed');
      };

      return () => {
        if (wsRef.current) {
          wsRef.current.close();
        }
      };
    }
  }, [currentView, selectedRunId]);

  // Create Run
  const handleCreateRun = async (e: React.FormEvent) => {
    e.preventDefault();
    let envVars = {};
    try {
      envVars = JSON.parse(runEnvVarsText);
    } catch (err) {
      alert('Invalid Env Vars JSON format');
      return;
    }

    setIsCreatingRun(true);
    try {
      const res = await fetch(`${getApiBase()}/api/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: runName || `Run: ${new Date().toLocaleString()}`,
          benchmark_id: runBenchmarkId,
          harness_type: runHarnessType,
          model: runModel,
          base_url: runBaseUrl || null,
          cli_command: runHarnessType === 'cli' ? runCliCommand : null,
          env_vars: envVars,
          concurrency: runConcurrency,
          timeout_seconds: runTimeout,
          recursion_limit: runRecursionLimit,
          global_token_budget: runGlobalTokenBudget,
        }),
      });

      if (res.ok) {
        const newRun = await res.json();
        setRunName('');
        setRunModel('');
        setRunBaseUrl('');
        setRunCliCommand('');
        fetchRuns();
        // Go to run details
        setSelectedRunId(newRun.id);
        fetchRunDetails(newRun.id);
        setCurrentView('run-details');
      } else {
        const errData = await res.json();
        alert(`Failed to create run: ${errData.detail || 'Unknown error'}`);
      }
    } catch (err) {
      console.error(err);
      alert('Error creating run');
    } finally {
      setIsCreatingRun(false);
    }
  };

  // Cancel Run
  const handleCancelRun = async (id: string) => {
    if (!confirm('Are you sure you want to cancel this run?')) return;
    try {
      const res = await fetch(`${getApiBase()}/api/runs/${id}/cancel`, { method: 'POST' });
      if (res.ok) {
        fetchRuns();
        if (selectedRunId === id) fetchRunDetails(id);
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Delete Run
  const handleDeleteRun = async (id: string) => {
    if (!confirm('Are you sure you want to delete this run? This will delete all task results.')) return;
    try {
      const res = await fetch(`${getApiBase()}/api/runs/${id}`, { method: 'DELETE' });
      if (res.ok) {
        fetchRuns();
        if (selectedRunId === id) {
          setSelectedRun(null);
          setSelectedRunId(null);
          setCurrentView('runs');
        }
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Override Task Status
  const handleOverrideStatus = async (taskResultId: string, status: string) => {
    if (!confirm(`Force status to ${status.toUpperCase()} for this task result?`)) return;
    try {
      const res = await fetch(`${getApiBase()}/api/runs/tasks/${taskResultId}/override-status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status })
      });
      if (res.ok) {
        if (selectedRunId) fetchRunDetails(selectedRunId);
        fetchRuns();
      } else {
        const errData = await res.json();
        alert(`Failed to override status: ${errData.detail || 'Unknown error'}`);
      }
    } catch (err) {
      console.error(err);
      alert('Error overriding status');
    }
  };

  // Create Benchmark
  const handleCreateBenchmark = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!bmName) return;
    setIsCreatingBenchmark(true);
    try {
      const res = await fetch(`${getApiBase()}/api/benchmarks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: bmName, description: bmDescription }),
      });
      if (res.ok) {
        const data = await res.json();
        setBmName('');
        setBmDescription('');
        fetchBenchmarks();
        fetchBenchmarkDetails(data.id);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setIsCreatingBenchmark(false);
    }
  };

  // Delete Benchmark
  const handleDeleteBenchmark = async (id: string) => {
    if (!confirm('Are you sure you want to delete this benchmark? All test definitions will be deleted.')) return;
    try {
      const res = await fetch(`${getApiBase()}/api/benchmarks/${id}`, { method: 'DELETE' });
      if (res.ok) {
        fetchBenchmarks();
        setSelectedBenchmark(null);
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Import Builtins
  const handleImportBuiltins = async (id: string) => {
    if (!confirm('Import all 298 built-in tasks? This will populate the benchmark groups and tasks.')) return;
    setIsCreatingBenchmark(true);
    try {
      const res = await fetch(`${getApiBase()}/api/benchmarks/${id}/import-builtin`, { method: 'POST' });
      if (res.ok) {
        fetchBenchmarkDetails(id);
      } else {
        alert('Import failed. Make sure harness-bench-fast is installed.');
      }
    } catch (err) {
      console.error(err);
    } finally {
      setIsCreatingBenchmark(false);
    }
  };

  // Create Group
  const handleCreateGroup = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!groupName || !selectedBenchmark) return;
    setIsCreatingGroup(true);
    try {
      const res = await fetch(`${getApiBase()}/api/benchmarks/${selectedBenchmark.id}/groups`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: groupName,
          description: groupDescription,
          default_token_budget: groupDefaultBudget,
          default_timeout: groupDefaultTimeout,
        }),
      });
      if (res.ok) {
        setGroupName('');
        setGroupDescription('');
        fetchBenchmarkDetails(selectedBenchmark.id);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setIsCreatingGroup(false);
    }
  };

  // Delete Group
  const handleDeleteGroup = async (groupId: string) => {
    if (!confirm('Delete this group?')) return;
    try {
      const res = await fetch(`${getApiBase()}/api/groups/${groupId}`, { method: 'DELETE' });
      if (res.ok && selectedBenchmark) {
        fetchBenchmarkDetails(selectedBenchmark.id);
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Create Test
  const handleCreateTest = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!testName || !testPrompt || !selectedGroupId) return;

    let tags = [];
    let setup = {};
    let gold = {};
    try {
      tags = JSON.parse(testTagsText);
      setup = JSON.parse(testSetupFilesText);
      gold = JSON.parse(testGoldFilesText);
    } catch (err) {
      alert('Invalid JSON in Tags, Setup files, or Gold files');
      return;
    }

    setIsCreatingTest(true);
    try {
      const res = await fetch(`${getApiBase()}/api/groups/${selectedGroupId}/tests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: testName,
          prompt: testPrompt,
          tags,
          setup_files: setup,
          gold_files: gold,
          verifier_type: testVerifierType || 'custom',
          verifier_config: {},
          token_budget: testTokenBudget,
          timeout_seconds: testTimeout,
        }),
      });
      if (res.ok) {
        setTestName('');
        setTestPrompt('');
        setTestTagsText('[]');
        setTestSetupFilesText('{}');
        setTestGoldFilesText('{}');
        setTestVerifierType('');
        if (selectedBenchmark) fetchBenchmarkDetails(selectedBenchmark.id);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setIsCreatingTest(false);
    }
  };

  // Delete Test
  const handleDeleteTest = async (testId: string) => {
    if (!confirm('Delete this test?')) return;
    try {
      const res = await fetch(`${getApiBase()}/api/tests/${testId}`, { method: 'DELETE' });
      if (res.ok && selectedBenchmark) {
        fetchBenchmarkDetails(selectedBenchmark.id);
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Compare Runs
  const handleCompare = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!compareId1 || !compareId2) return;
    setIsComparing(true);
    try {
      const res = await fetch(`${getApiBase()}/api/runs/compare?run_id_1=${compareId1}&run_id_2=${compareId2}`);
      if (res.ok) {
        const data = await res.json();
        setCompareResult(data);
      } else {
        alert('Comparison failed');
      }
    } catch (err) {
      console.error(err);
    } finally {
      setIsComparing(false);
    }
  };

  // Roman Numeral Helper
  const getRoman = (num: number) => {
    const lookup: { [key: string]: number } = { M: 1000, CM: 900, D: 500, CD: 400, C: 100, XC: 90, L: 50, XL: 40, X: 10, IX: 9, V: 5, IV: 4, I: 1 };
    let roman = '';
    let i;
    for (i in lookup) {
      while (num >= lookup[i]) {
        roman += i;
        num -= lookup[i];
      }
    }
    return roman || 'O';
  };

  // Relative Time Helper
  const getRelativeTime = (dateStr: string) => {
    if (!dateStr) return '◆ NEVER';
    const date = new Date(dateStr);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHour = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHour / 24);

    if (diffSec < 60) return '◆ JUST NOW';
    if (diffMin < 60) return `◆ ${diffMin} M AGO`;
    if (diffHour < 24) return `◆ ${diffHour} H AGO`;
    return `◆ ${diffDay} D AGO`;
  };

  return (
    <div className="container">
      {/* Header */}
      <header className="header">
        <div>
          <h1 className="header-title">Harness Bench Fast</h1>
          <div className="header-subtitle">◆ WEB MANAGEMENT CONSOLE ◆ EST. 2026</div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '1.5rem' }}>
          <nav className="nav-links">
            <button
              onClick={() => setCurrentView('runs')}
              className={`btn nav-link ${currentView === 'runs' || currentView === 'run-details' ? 'active' : ''}`}
              style={{ border: 'none' }}
            >
              Runs
            </button>
            <button
              onClick={() => {
                setCurrentView('benchmarks');
                if (benchmarks.length > 0 && !selectedBenchmark) {
                  fetchBenchmarkDetails(benchmarks[0].id);
                }
              }}
              className={`btn nav-link ${currentView === 'benchmarks' ? 'active' : ''}`}
              style={{ border: 'none' }}
            >
              Benchmarks
            </button>
            <button
              onClick={() => setCurrentView('compare')}
              className={`btn nav-link ${currentView === 'compare' ? 'active' : ''}`}
              style={{ border: 'none' }}
            >
              Compare
            </button>
          </nav>
          <button className="theme-toggle" onClick={toggleTheme}>
            {theme === 'light' ? '■ Dark Mode' : '□ Light Mode'}
          </button>
        </div>
      </header>

      {/* VIEW: RUNS */}
      {currentView === 'runs' && (
        <div style={{ display: 'grid', gridTemplateColumns: '1.8fr 1.2fr', gap: '2rem' }}>
          {/* Left Column: All Runs List */}
          <div>
            <h2 style={{ fontSize: '2rem', marginBottom: '1.5rem', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.5rem' }}>
              Execution History
            </h2>
            <div className="grid" style={{ gridTemplateColumns: '1fr' }}>
              {runs.length === 0 ? (
                <div style={{ padding: '2rem', textAlign: 'center', border: '1px dashed var(--border-color)', color: 'var(--text-dimmed)', fontFamily: 'var(--font-mono)' }}>
                  NO BENCHMARK RUNS RECORDED YET
                </div>
              ) : (
                runs.map((r, idx) => (
                  <div key={r.id} className="card" style={{ cursor: 'pointer' }} onClick={() => {
                    setSelectedRunId(r.id);
                    fetchRunDetails(r.id);
                    setCurrentView('run-details');
                  }}>
                    <div className="card-header">
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <h3 className="card-title">
                          <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.9rem', marginRight: '0.5rem', color: 'var(--text-dimmed)' }}>
                            [{getRoman(runs.length - idx)}]
                          </span>
                          {r.name}
                        </h3>
                        <span className={`badge badge-${r.status}`}>
                          {r.status}
                        </span>
                      </div>
                      <div className="card-meta">
                        <span>Model: {r.model || 'Default'} ({r.harness_type})</span>
                        <span>{getRelativeTime(r.created_at)}</span>
                      </div>
                    </div>
                    
                    <div className="card-body" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', fontFamily: 'var(--font-mono)', fontSize: '0.8rem', marginBottom: '1rem' }}>
                      <div>
                        <div>Tasks completed: {r.completed_tasks} / {r.total_tasks}</div>
                        <div>Passed: <span style={{ color: 'var(--green-color)' }}>{r.passed_tasks}</span> | Failed: <span style={{ color: 'var(--red-color)' }}>{r.failed_tasks}</span></div>
                      </div>
                      <div>
                        <div>Total tokens: {r.total_tokens?.toLocaleString() || 0}</div>
                        <div>Avg tokens/sec: {r.avg_tokens_per_second ? r.avg_tokens_per_second.toFixed(1) : 0.0}</div>
                      </div>
                    </div>

                    <div style={{ display: 'flex', gap: '0.5rem', alignSelf: 'flex-end' }} onClick={(e) => e.stopPropagation()}>
                      {r.status === 'running' && (
                        <button className="btn btn-danger" onClick={() => handleCancelRun(r.id)}>
                          Cancel
                        </button>
                      )}
                      <button className="btn btn-danger" onClick={() => handleDeleteRun(r.id)}>
                        Delete
                      </button>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Right Column: Launch New Run */}
          <div>
            <h2 style={{ fontSize: '2rem', marginBottom: '1.5rem', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.5rem' }}>
              Launch Benchmark
            </h2>
            <form onSubmit={handleCreateRun} className="card" style={{ backgroundColor: 'var(--card-bg)' }}>
              <div className="form-group">
                <label className="form-label">Run Name</label>
                <input
                  type="text"
                  className="form-input"
                  placeholder="e.g. GigaChat Ultra Run"
                  value={runName}
                  onChange={(e) => setRunName(e.target.value)}
                />
              </div>

              <div className="form-group">
                <label className="form-label">Benchmark Suite</label>
                <select
                  className="form-select"
                  value={runBenchmarkId}
                  onChange={(e) => setRunBenchmarkId(e.target.value)}
                  required
                >
                  {benchmarks.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name} ({b.total_tests} tasks)
                    </option>
                  ))}
                  {benchmarks.length === 0 && <option value="">No benchmarks available</option>}
                </select>
              </div>

              <div className="form-group">
                <label className="form-label">Harness Type</label>
                <select
                  className="form-select"
                  value={runHarnessType}
                  onChange={(e) => setRunHarnessType(e.target.value)}
                >
                  <option value="cli">CLI Runner (Shell command)</option>
                  <option value="deepagents">DeepAgents (with GigaChat profile)</option>
                  <option value="pure">Pure DeepAgents (without GigaChat profile)</option>
                  <option value="openrouter">OpenRouter (Third-party models)</option>
                </select>
              </div>

              <div className="form-group">
                <label className="form-label">Model Name Override</label>
                <input
                  type="text"
                  className="form-input"
                  placeholder="e.g. GigaChat-3-Ultra / qwen/qwen3.6-plus"
                  value={runModel}
                  onChange={(e) => setRunModel(e.target.value)}
                />
              </div>

              <div className="form-group">
                <label className="form-label">Base URL Override</label>
                <input
                  type="text"
                  className="form-input"
                  placeholder="e.g. https://api.openrouter.ai/v1"
                  value={runBaseUrl}
                  onChange={(e) => setRunBaseUrl(e.target.value)}
                />
              </div>

              {runHarnessType === 'cli' && (
                <div className="form-group">
                  <label className="form-label">CLI Command template</label>
                  <input
                    type="text"
                    className="form-input"
                    placeholder="free-code -p --model haiku"
                    value={runCliCommand}
                    onChange={(e) => setRunCliCommand(e.target.value)}
                    required
                  />
                </div>
              )}

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                <div className="form-group">
                  <label className="form-label">Concurrency</label>
                  <input
                    type="number"
                    className="form-input"
                    value={runConcurrency}
                    onChange={(e) => setRunConcurrency(parseInt(e.target.value))}
                    min={1}
                    max={20}
                  />
                </div>
                <div className="form-group">
                  <label className="form-label">Task Timeout (s)</label>
                  <input
                    type="number"
                    className="form-input"
                    value={runTimeout}
                    onChange={(e) => setRunTimeout(parseInt(e.target.value))}
                    min={10}
                  />
                </div>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                <div className="form-group">
                  <label className="form-label">Agent steps limit</label>
                  <input
                    type="number"
                    className="form-input"
                    value={runRecursionLimit}
                    onChange={(e) => setRunRecursionLimit(parseInt(e.target.value))}
                    min={1}
                  />
                </div>
                <div className="form-group">
                  <label className="form-label">Global Token Budget</label>
                  <input
                    type="number"
                    className="form-input"
                    value={runGlobalTokenBudget}
                    onChange={(e) => setRunGlobalTokenBudget(parseInt(e.target.value))}
                    placeholder="-1 (No limit)"
                  />
                </div>
              </div>

              <div className="form-group">
                <label className="form-label">Env Variables Override (JSON)</label>
                <textarea
                  className="form-textarea"
                  style={{ minHeight: '80px', fontFamily: 'var(--font-mono)', fontSize: '0.8rem' }}
                  value={runEnvVarsText}
                  onChange={(e) => setRunEnvVarsText(e.target.value)}
                />
              </div>

              <button type="submit" className="btn btn-primary" style={{ marginTop: '0.5rem', width: '100%' }} disabled={isCreatingRun}>
                {isCreatingRun ? 'LAUNCHING...' : 'LAUNCH RUN'}
              </button>
            </form>
          </div>
        </div>
      )}

      {/* VIEW: RUN DETAILS */}
      {currentView === 'run-details' && selectedRunId && (
        <div>
          <button className="btn" style={{ marginBottom: '1.5rem' }} onClick={() => { setCurrentView('runs'); setSelectedRun(null); setSelectedRunId(null); }}>
            ← Back to Runs
          </button>
          
          {selectedRun && (
            <div>
              {/* Header stats card */}
              <div className="card" style={{ marginBottom: '2rem' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
                  <h2 style={{ fontSize: '2.2rem' }}>{selectedRun.name}</h2>
                  <span className={`badge badge-${selectedRun.status}`} style={{ fontSize: '0.9rem', padding: '0.3rem 0.6rem' }}>
                    {selectedRun.status}
                  </span>
                </div>

                <div className="progress-bar-container" style={{ margin: '1rem 0 1.5rem 0', height: '6px' }}>
                  <div
                    className="progress-bar-fill"
                    style={{
                      width: `${selectedRun.total_tasks > 0 ? (selectedRun.completed_tasks / selectedRun.total_tasks) * 100 : 0}%`
                    }}
                  />
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '1rem', fontFamily: 'var(--font-mono)', fontSize: '0.9rem' }}>
                  <div>
                    <div style={{ color: 'var(--text-dimmed)', fontSize: '0.75rem', textTransform: 'uppercase' }}>Harness & Model</div>
                    <div style={{ fontWeight: 'bold' }}>{selectedRun.harness_type} ◆ {selectedRun.model || 'default'}</div>
                  </div>
                  <div>
                    <div style={{ color: 'var(--text-dimmed)', fontSize: '0.75rem', textTransform: 'uppercase' }}>Tasks progress</div>
                    <div style={{ fontWeight: 'bold' }}>{selectedRun.completed_tasks} / {selectedRun.total_tasks}</div>
                  </div>
                  <div>
                    <div style={{ color: 'var(--text-dimmed)', fontSize: '0.75rem', textTransform: 'uppercase' }}>Passed / Failed</div>
                    <div style={{ fontWeight: 'bold' }}>
                      <span style={{ color: 'var(--green-color)' }}>{selectedRun.passed_tasks}</span> / <span style={{ color: 'var(--red-color)' }}>{selectedRun.failed_tasks}</span>
                    </div>
                  </div>
                  <div>
                    <div style={{ color: 'var(--text-dimmed)', fontSize: '0.75rem', textTransform: 'uppercase' }}>Tokens Spent (avg speed)</div>
                    <div style={{ fontWeight: 'bold' }}>
                      {selectedRun.total_tokens?.toLocaleString() || 0} ({selectedRun.avg_tokens_per_second?.toFixed(1) || 0.0} t/s)
                    </div>
                  </div>
                </div>

                {selectedRun.status === 'running' && (
                  <div style={{ marginTop: '1rem', borderTop: '1px dashed var(--border-color)', paddingTop: '1rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div style={{ fontFamily: 'var(--font-mono)', fontSize: '0.85rem' }}>
                      ⏳ Realtime connection active. Running concurrency: {selectedRun.concurrency}
                    </div>
                    <button className="btn btn-danger" onClick={() => handleCancelRun(selectedRun.id)}>
                      Cancel Execution
                    </button>
                  </div>
                )}
              </div>

              {/* Tasks List Outcomes header */}
              <h3 style={{ fontSize: '1.6rem', marginBottom: '1rem' }}>Task Outcomes</h3>

              {/* Tabs Selector */}
              <div style={{ display: 'flex', gap: '1rem', borderBottom: '1px solid var(--border-color)', marginBottom: '1.5rem' }}>
                <button
                  className={`btn nav-link ${runDetailTab === 'table' ? 'active' : ''}`}
                  style={{ border: 'none', paddingBottom: '0.75rem', borderRadius: '0', borderBottom: runDetailTab === 'table' ? '2px solid var(--accent-color)' : 'none', fontWeight: 600, backgroundColor: 'transparent' }}
                  onClick={() => setRunDetailTab('table')}
                >
                  Ledger View (Table)
                </button>
                <button
                  className={`btn nav-link ${runDetailTab === 'console' ? 'active' : ''}`}
                  style={{ border: 'none', paddingBottom: '0.75rem', borderRadius: '0', borderBottom: runDetailTab === 'console' ? '2px solid var(--accent-color)' : 'none', fontWeight: 600, backgroundColor: 'transparent' }}
                  onClick={() => setRunDetailTab('console')}
                >
                  Live Terminal Console
                </button>
              </div>

              {runDetailTab === 'console' ? (
                <div className="card" style={{ backgroundColor: '#121212', border: '1px solid #333', padding: '1.5rem', borderRadius: '4px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem', borderBottom: '1px dashed #333', paddingBottom: '0.5rem' }}>
                    <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent-color)', fontSize: '0.9rem', fontWeight: 'bold' }}>
                      [SYSTEM TERMINAL CONSOLE LOG STREAM]
                    </span>
                    <span style={{ fontFamily: 'var(--font-mono)', color: '#666', fontSize: '0.8rem' }}>
                      RUN_ID: {selectedRun.id}
                    </span>
                  </div>
                  
                  <div style={{
                    maxHeight: '650px',
                    overflowY: 'auto',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.85rem',
                    color: '#ddd',
                    lineHeight: '1.5',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '1.5rem',
                    paddingRight: '0.5rem'
                  }}>
                    {selectedRun.task_results?.filter((t: any) => t.status !== 'pending' && t.status !== 'running').length === 0 ? (
                      <div style={{ color: '#666', fontStyle: 'italic', textAlign: 'center', padding: '2rem' }}>
                        NO EXECUTION DATA STREAMED YET
                      </div>
                    ) : (
                      selectedRun.task_results?.filter((t: any) => t.status !== 'pending').map((t: any, idx: number) => {
                        const startStr = t.started_at ? new Date(t.started_at).toLocaleTimeString() : 'N/A';
                        const endStr = t.finished_at ? new Date(t.finished_at).toLocaleTimeString() : 'N/A';
                        const durationStr = t.elapsed_seconds ? `${t.elapsed_seconds.toFixed(1)}s` : '';
                        
                        // Reconstruct command line
                        const cmd = selectedRun.harness_type === 'cli'
                          ? `${selectedRun.cli_command || 'hermes'} "${t.prompt || t.task_name}"`
                          : `runner_${selectedRun.harness_type} --model "${selectedRun.model || 'default'}" --prompt "${t.prompt || t.task_name}"`;

                        const outcomeColor = t.status === 'passed' ? 'var(--green-color)' : 'var(--red-color)';

                        return (
                          <div key={t.id} style={{ borderBottom: '1px solid #222', paddingBottom: '1rem' }}>
                            {/* Invocation */}
                            <div style={{ color: '#888', marginBottom: '0.5rem' }}>
                              <span style={{ color: 'var(--accent-color)' }}>[{startStr}]</span> INVOKING: <span style={{ color: '#fff' }}>{cmd}</span>
                            </div>
                            
                            {/* Output */}
                            {(t.message || t.error_detail) ? (
                              <pre style={{
                                margin: '0.5rem 0',
                                padding: '0.75rem',
                                backgroundColor: '#181818',
                                border: '1px solid #222',
                                color: '#bbb',
                                fontSize: '0.8rem',
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-all'
                              }}>
                                {t.message && `[Verifier Output]:\n${t.message}\n\n`}
                                {t.error_detail && `[Run Log / Messages]:\n${t.error_detail}`}
                              </pre>
                            ) : (
                              <div style={{ color: '#555', fontStyle: 'italic', fontSize: '0.8rem', margin: '0.5rem 0 0.5rem 1rem' }}>
                                No stdout or transcript logs recorded.
                              </div>
                            )}
                            
                            {/* Status */}
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '0.5rem' }}>
                              <div style={{ color: outcomeColor, fontWeight: 'bold' }}>
                                [{endStr}] STATUS: {t.status.toUpperCase()} ({durationStr} {t.agent_total_tokens ? `| ${t.agent_total_tokens.toLocaleString()} tokens` : ''})
                              </div>
                              <div style={{ display: 'flex', gap: '0.5rem' }}>
                                {t.status !== 'passed' && (
                                  <button
                                    className="btn"
                                    style={{ borderColor: 'var(--green-color)', color: 'var(--green-color)', fontSize: '0.65rem', padding: '0.1rem 0.4rem', height: 'auto', lineHeight: '1' }}
                                    onClick={() => handleOverrideStatus(t.id, 'passed')}
                                  >
                                    ✓ FORCE PASS
                                  </button>
                                )}
                                {t.status !== 'failed' && (
                                  <button
                                    className="btn"
                                    style={{ borderColor: 'var(--red-color)', color: 'var(--red-color)', fontSize: '0.65rem', padding: '0.1rem 0.4rem', height: 'auto', lineHeight: '1' }}
                                    onClick={() => handleOverrideStatus(t.id, 'failed')}
                                  >
                                    ✗ FORCE FAIL
                                  </button>
                                )}
                              </div>
                            </div>
                          </div>
                        );
                      })
                    )}
                  </div>
                </div>
              ) : (
                <div className="table-container">
                  <table className="table">
                    <thead>
                      <tr>
                        <th style={{ width: '60px' }}>No.</th>
                        <th>Task Name</th>
                        <th>Status</th>
                        <th>Tokens</th>
                        <th>T/s</th>
                        <th>Time</th>
                        <th>Result details / Error message</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedRun.task_results?.map((t: any, idx: number) => (
                        <React.Fragment key={t.id}>
                          <tr>
                            <td style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-dimmed)' }}>{idx + 1}</td>
                            <td style={{ fontWeight: 600 }}>{t.task_name}</td>
                            <td>
                              <span className={`badge badge-${t.status}`}>{t.status}</span>
                            </td>
                            <td style={{ fontFamily: 'var(--font-mono)' }}>{t.agent_total_tokens?.toLocaleString() || '-'}</td>
                            <td style={{ fontFamily: 'var(--font-mono)' }}>{t.tokens_per_second ? t.tokens_per_second.toFixed(1) : '-'}</td>
                            <td style={{ fontFamily: 'var(--font-mono)' }}>{t.elapsed_seconds ? `${t.elapsed_seconds.toFixed(1)}s` : '-'}</td>
                            <td style={{ fontSize: '0.85rem' }}>
                              <div style={{ maxWidth: '400px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {t.message || t.error_detail || '-'}
                              </div>
                            </td>
                          </tr>
                          {(t.message || t.error_detail) && (
                            <tr style={{ backgroundColor: 'rgba(0,0,0,0.02)' }}>
                              <td colSpan={7} style={{ padding: '0.5rem 1rem 1rem 3rem', fontSize: '0.8rem', borderTop: 'none' }}>
                                <details open>
                                  <summary style={{ fontFamily: 'var(--font-mono)', cursor: 'pointer', color: 'var(--text-dimmed)', marginBottom: '0.5rem' }}>
                                    View execution details & verifier logs
                                  </summary>
                                  <div style={{ border: '1px solid var(--border-color)', padding: '1rem', backgroundColor: 'var(--card-bg)', borderRadius: '4px' }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                                      <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-dimmed)', fontSize: '0.75rem' }}>
                                        ◆ TERMINAL CONSOLE / AGENT TRANSCRIPT ◆
                                      </span>
                                      <div style={{ display: 'flex', gap: '0.5rem' }}>
                                        {t.status !== 'passed' && (
                                          <button
                                            className="btn"
                                            style={{ borderColor: 'var(--green-color)', color: 'var(--green-color)', fontSize: '0.7rem', padding: '0.2rem 0.6rem', height: 'auto', lineHeight: '1' }}
                                            onClick={() => handleOverrideStatus(t.id, 'passed')}
                                          >
                                            ✓ FORCE PASS
                                          </button>
                                        )}
                                        {t.status !== 'failed' && (
                                          <button
                                            className="btn"
                                            style={{ borderColor: 'var(--red-color)', color: 'var(--red-color)', fontSize: '0.7rem', padding: '0.2rem 0.6rem', height: 'auto', lineHeight: '1' }}
                                            onClick={() => handleOverrideStatus(t.id, 'failed')}
                                          >
                                            ✗ FORCE FAIL
                                          </button>
                                        )}
                                      </div>
                                    </div>
                                    <pre style={{
                                      whiteSpace: 'pre-wrap',
                                      wordBreak: 'break-all',
                                      fontFamily: 'var(--font-mono)',
                                      padding: '1rem',
                                      border: '1px solid var(--border-color)',
                                      backgroundColor: 'var(--card-bg)',
                                      maxHeight: '350px',
                                      overflowY: 'auto',
                                      borderRadius: '4px',
                                      fontSize: '0.8rem',
                                      lineHeight: '1.4'
                                    }}>
                                      {t.message && `[Verifier Output]:\n${t.message}\n\n`}
                                      {t.error_detail && `[Run Log / Messages]:\n${t.error_detail}`}
                                    </pre>
                                  </div>
                                </details>
                              </td>
                            </tr>
                          )}
                        </React.Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* VIEW: BENCHMARKS */}
      {currentView === 'benchmarks' && (
        <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1.8fr', gap: '2rem' }}>
          {/* Left Panel: Benchmarks List & Create */}
          <div>
            <h2 style={{ fontSize: '2rem', marginBottom: '1.5rem', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.5rem' }}>
              Benchmark Suites
            </h2>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', marginBottom: '2rem' }}>
              {benchmarks.map((b) => (
                <div
                  key={b.id}
                  className={`card ${selectedBenchmark?.id === b.id ? 'active-card' : ''}`}
                  style={{
                    cursor: 'pointer',
                    borderLeftWidth: selectedBenchmark?.id === b.id ? '4px' : '1px',
                    borderLeftColor: selectedBenchmark?.id === b.id ? 'var(--accent-color)' : 'var(--border-color)'
                  }}
                  onClick={() => fetchBenchmarkDetails(b.id)}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <h3 className="card-title">{b.name}</h3>
                    <button
                      className="btn btn-danger"
                      style={{ padding: '0.2rem 0.5rem', fontSize: '0.65rem' }}
                      onClick={(e) => { e.stopPropagation(); handleDeleteBenchmark(b.id); }}
                    >
                      Delete
                    </button>
                  </div>
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-dimmed)', marginTop: '0.25rem' }}>{b.description || 'No description'}</p>
                  <div style={{ fontFamily: 'var(--font-mono)', fontSize: '0.75rem', marginTop: '0.75rem', color: 'var(--text-dimmed)' }}>
                    Groups: {b.group_count} | Total tasks: {b.total_tests}
                  </div>
                </div>
              ))}
            </div>

            <form onSubmit={handleCreateBenchmark} className="card" style={{ backgroundColor: 'var(--card-bg)' }}>
              <h3 style={{ fontSize: '1.3rem', marginBottom: '1rem' }}>New Suite</h3>
              <div className="form-group">
                <label className="form-label">Name</label>
                <input
                  type="text"
                  className="form-input"
                  value={bmName}
                  onChange={(e) => setBmName(e.target.value)}
                  placeholder="e.g. Custom Agent Benchmark"
                  required
                />
              </div>
              <div className="form-group">
                <label className="form-label">Description</label>
                <input
                  type="text"
                  className="form-input"
                  value={bmDescription}
                  onChange={(e) => setBmDescription(e.target.value)}
                  placeholder="Suite description"
                />
              </div>
              <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={isCreatingBenchmark}>
                CREATE SUITE
              </button>
            </form>
          </div>

          {/* Right Panel: Selected Benchmark details, Groups and Tests */}
          <div>
            {selectedBenchmark ? (
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '1rem', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.5rem' }}>
                  <div>
                    <h2 style={{ fontSize: '2.2rem' }}>{selectedBenchmark.name}</h2>
                    <p style={{ color: 'var(--text-dimmed)', fontSize: '0.9rem' }}>{selectedBenchmark.description}</p>
                  </div>
                  <button className="btn btn-primary" onClick={() => handleImportBuiltins(selectedBenchmark.id)} disabled={isCreatingBenchmark}>
                    Import 298 Builtin Tasks
                  </button>
                </div>

                {/* Groups structure */}
                <div>
                  <h3 style={{ fontSize: '1.6rem', margin: '2rem 0 1rem 0' }}>Test Groups</h3>
                  
                  {selectedBenchmark.groups?.length === 0 ? (
                    <div style={{ padding: '2rem', textAlign: 'center', border: '1px dashed var(--border-color)', color: 'var(--text-dimmed)', fontFamily: 'var(--font-mono)', marginBottom: '2rem' }}>
                      THIS SUITE IS EMPTY. ADD GROUPS AND TESTS BELOW OR IMPORT BUILTINS.
                    </div>
                  ) : (
                    selectedBenchmark.groups?.map((g: any) => (
                      <div key={g.id} className="card" style={{ marginBottom: '1.5rem', backgroundColor: 'var(--card-bg)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.5rem', marginBottom: '0.75rem' }}>
                          <h4 style={{ fontSize: '1.3rem' }}>
                            {g.name} 
                            <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.75rem', marginLeft: '0.5rem', color: 'var(--text-dimmed)' }}>
                              ({g.tests?.length || 0} tests)
                            </span>
                          </h4>
                          <button className="btn btn-danger" style={{ padding: '0.2rem 0.5rem', fontSize: '0.65rem' }} onClick={() => handleDeleteGroup(g.id)}>
                            Delete Group
                          </button>
                        </div>
                        
                        <div style={{ fontFamily: 'var(--font-mono)', fontSize: '0.75rem', color: 'var(--text-dimmed)', marginBottom: '1rem' }}>
                          Default timeout: {g.default_timeout}s | Default budget: {g.default_token_budget > 0 ? g.default_token_budget : 'unlimited'}
                        </div>

                        {g.tests?.length > 0 ? (
                          <div className="table-container" style={{ marginTop: '0.5rem' }}>
                            <table className="table" style={{ fontSize: '0.85rem' }}>
                              <thead>
                                <tr>
                                  <th>Task Name</th>
                                  <th>Prompt / Test Intent</th>
                                  <th>Verifier</th>
                                  <th>Actions</th>
                                </tr>
                              </thead>
                              <tbody>
                                {g.tests.map((t: any) => (
                                  <tr key={t.id}>
                                    <td style={{ fontWeight: 600, width: '150px' }}>{t.name}</td>
                                    <td style={{ color: 'var(--text-dimmed)' }}>{t.prompt}</td>
                                    <td style={{ fontFamily: 'var(--font-mono)', width: '120px' }}>{t.verifier_type}</td>
                                    <td style={{ width: '80px' }}>
                                      <button className="btn btn-danger" style={{ padding: '0.2rem 0.4rem', fontSize: '0.65rem' }} onClick={() => handleDeleteTest(t.id)}>
                                        Delete
                                      </button>
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        ) : (
                          <div style={{ padding: '1rem', textAlign: 'center', border: '1px dashed var(--border-color)', color: 'var(--text-dimmed)', fontSize: '0.8rem' }}>
                            No tests in this group.
                          </div>
                        )}
                      </div>
                    ))
                  )}

                  {/* Add Group & Add Test forms side by side */}
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2rem', marginTop: '2rem' }}>
                    {/* Add Group Form */}
                    <form onSubmit={handleCreateGroup} className="card" style={{ alignSelf: 'flex-start' }}>
                      <h4 style={{ fontSize: '1.2rem', marginBottom: '1rem' }}>Add Test Group</h4>
                      <div className="form-group">
                        <label className="form-label">Group Name</label>
                        <input
                          type="text"
                          className="form-input"
                          value={groupName}
                          onChange={(e) => setGroupName(e.target.value)}
                          placeholder="e.g. Memory tests"
                          required
                        />
                      </div>
                      <div className="form-group">
                        <label className="form-label">Description</label>
                        <input
                          type="text"
                          className="form-input"
                          value={groupDescription}
                          onChange={(e) => setGroupDescription(e.target.value)}
                          placeholder="Group description"
                        />
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                        <div className="form-group">
                          <label className="form-label">Default Budget</label>
                          <input
                            type="number"
                            className="form-input"
                            value={groupDefaultBudget}
                            onChange={(e) => setGroupDefaultBudget(parseInt(e.target.value))}
                          />
                        </div>
                        <div className="form-group">
                          <label className="form-label">Default Timeout</label>
                          <input
                            type="number"
                            className="form-input"
                            value={groupDefaultTimeout}
                            onChange={(e) => setGroupDefaultTimeout(parseInt(e.target.value))}
                          />
                        </div>
                      </div>
                      <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={isCreatingGroup}>
                        ADD GROUP
                      </button>
                    </form>

                    {/* Add Test Form */}
                    {selectedBenchmark.groups?.length > 0 && (
                      <form onSubmit={handleCreateTest} className="card">
                        <h4 style={{ fontSize: '1.2rem', marginBottom: '1rem' }}>Add Test Case</h4>
                        <div className="form-group">
                          <label className="form-label">Target Group</label>
                          <select
                            className="form-select"
                            value={selectedGroupId}
                            onChange={(e) => setSelectedGroupId(e.target.value)}
                            required
                          >
                            <option value="">Select a group</option>
                            {selectedBenchmark.groups.map((g: any) => (
                              <option key={g.id} value={g.id}>{g.name}</option>
                            ))}
                          </select>
                        </div>
                        <div className="form-group">
                          <label className="form-label">Test Name</label>
                          <input
                            type="text"
                            className="form-input"
                            value={testName}
                            onChange={(e) => setTestName(e.target.value)}
                            placeholder="e.g. Test memory task 01"
                            required
                          />
                        </div>
                        <div className="form-group">
                          <label className="form-label">Prompt</label>
                          <textarea
                            className="form-textarea"
                            style={{ minHeight: '60px' }}
                            value={testPrompt}
                            onChange={(e) => setTestPrompt(e.target.value)}
                            placeholder="Prompt task instructions for the agent"
                            required
                          />
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                          <div className="form-group">
                            <label className="form-label">Tags (JSON Array)</label>
                            <input
                              type="text"
                              className="form-input"
                              value={testTagsText}
                              onChange={(e) => setTestTagsText(e.target.value)}
                            />
                          </div>
                          <div className="form-group">
                            <label className="form-label">Verifier type</label>
                            <input
                              type="text"
                              className="form-input"
                              value={testVerifierType}
                              onChange={(e) => setTestVerifierType(e.target.value)}
                              placeholder="e.g. check_file / exit_code"
                            />
                          </div>
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                          <div className="form-group">
                            <label className="form-label">Setup files (JSON)</label>
                            <input
                              type="text"
                              className="form-input"
                              value={testSetupFilesText}
                              onChange={(e) => setTestSetupFilesText(e.target.value)}
                            />
                          </div>
                          <div className="form-group">
                            <label className="form-label">Gold files (JSON)</label>
                            <input
                              type="text"
                              className="form-input"
                              value={testGoldFilesText}
                              onChange={(e) => setTestGoldFilesText(e.target.value)}
                            />
                          </div>
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                          <div className="form-group">
                            <label className="form-label">Token budget</label>
                            <input
                              type="number"
                              className="form-input"
                              value={testTokenBudget}
                              onChange={(e) => setTestTokenBudget(parseInt(e.target.value))}
                            />
                          </div>
                          <div className="form-group">
                            <label className="form-label">Timeout (s)</label>
                            <input
                              type="number"
                              className="form-input"
                              value={testTimeout}
                              onChange={(e) => setTestTimeout(parseInt(e.target.value))}
                            />
                          </div>
                        </div>
                        <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={isCreatingTest}>
                          ADD TEST CASE
                        </button>
                      </form>
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <div style={{ padding: '3rem', textAlign: 'center', border: '1px dashed var(--border-color)', color: 'var(--text-dimmed)', fontFamily: 'var(--font-mono)' }}>
                SELECT A BENCHMARK SUITE OR CREATE A NEW ONE
              </div>
            )}
          </div>
        </div>
      )}

      {/* VIEW: COMPARE */}
      {currentView === 'compare' && (
        <div>
          <h2 style={{ fontSize: '2rem', marginBottom: '1.5rem', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.5rem' }}>
            Compare Runs
          </h2>

          <form onSubmit={handleCompare} className="card" style={{ marginBottom: '2.5rem', backgroundColor: 'var(--card-bg)' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2rem' }}>
              <div className="form-group">
                <label className="form-label">Run Reference A</label>
                <select className="form-select" value={compareId1} onChange={(e) => setCompareId1(e.target.value)} required>
                  <option value="">Select first run</option>
                  {runs.map((r) => (
                    <option key={r.id} value={r.id}>{r.name} ({r.model || 'default'})</option>
                  ))}
                </select>
              </div>

              <div className="form-group">
                <label className="form-label">Run Reference B</label>
                <select className="form-select" value={compareId2} onChange={(e) => setCompareId2(e.target.value)} required>
                  <option value="">Select second run</option>
                  {runs.map((r) => (
                    <option key={r.id} value={r.id}>{r.name} ({r.model || 'default'})</option>
                  ))}
                </select>
              </div>
            </div>
            <button type="submit" className="btn btn-primary" style={{ display: 'block', margin: '0.5rem auto 0 auto', width: '200px' }} disabled={isComparing}>
              {isComparing ? 'ANALYZING...' : 'COMPARE RUNS'}
            </button>
          </form>

          {compareResult && (
            <div>
              {/* Aggregated comparison card */}
              <div className="compare-header">
                <div className="compare-card">
                  <h3 style={{ fontSize: '1.6rem', marginBottom: '1rem', borderBottom: '1px solid var(--border-color)' }}>
                    [A] {compareResult.run_1.name}
                  </h3>
                  <div style={{ fontFamily: 'var(--font-mono)', fontSize: '0.9rem', lineHeight: '1.8' }}>
                    <div>Model: {compareResult.run_1.model || 'default'}</div>
                    <div>Harness: {compareResult.run_1.harness_type}</div>
                    <div>Passed tasks: <span style={{ color: 'var(--green-color)', fontWeight: 'bold' }}>{compareResult.run_1.passed_tasks}</span> / {compareResult.run_1.total_tasks}</div>
                    <div>Tokens used: {compareResult.run_1.total_tokens?.toLocaleString() || 0}</div>
                    <div>Speed: {compareResult.run_1.avg_tokens_per_second?.toFixed(1) || 0} tokens/s</div>
                  </div>
                </div>

                <div className="compare-card">
                  <h3 style={{ fontSize: '1.6rem', marginBottom: '1rem', borderBottom: '1px solid var(--border-color)' }}>
                    [B] {compareResult.run_2.name}
                  </h3>
                  <div style={{ fontFamily: 'var(--font-mono)', fontSize: '0.9rem', lineHeight: '1.8' }}>
                    <div>Model: {compareResult.run_2.model || 'default'}</div>
                    <div>Harness: {compareResult.run_2.harness_type}</div>
                    <div>Passed tasks: <span style={{ color: 'var(--green-color)', fontWeight: 'bold' }}>{compareResult.run_2.passed_tasks}</span> / {compareResult.run_2.total_tasks}</div>
                    <div>Tokens used: {compareResult.run_2.total_tokens?.toLocaleString() || 0}</div>
                    <div>Speed: {compareResult.run_2.avg_tokens_per_second?.toFixed(1) || 0} tokens/s</div>
                  </div>
                </div>
              </div>

              {/* Task by Task Diff Table */}
              <h3 className="compare-section-title" style={{ fontSize: '1.6rem' }}>Per-Task Comparison</h3>
              <div className="table-container">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Task Name</th>
                      <th style={{ textAlign: 'center' }}>Status A</th>
                      <th style={{ textAlign: 'center' }}>Status B</th>
                      <th style={{ textAlign: 'center' }}>Diff Status</th>
                      <th style={{ textAlign: 'right' }}>Tokens A</th>
                      <th style={{ textAlign: 'right' }}>Tokens B</th>
                      <th style={{ textAlign: 'right' }}>Diff Tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {compareResult.tasks.map((task: any) => {
                      const statusChanged = task.status_1 !== task.status_2;
                      const tokenDiff = (task.tokens_2 || 0) - (task.tokens_1 || 0);
                      return (
                        <tr key={task.task_name} style={{ backgroundColor: statusChanged ? 'rgba(212, 118, 58, 0.05)' : 'transparent' }}>
                          <td style={{ fontWeight: 600 }}>{task.task_name}</td>
                          <td style={{ textAlign: 'center' }}>
                            <span className={`badge badge-${task.status_1}`}>{task.status_1 || 'missing'}</span>
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            <span className={`badge badge-${task.status_2}`}>{task.status_2 || 'missing'}</span>
                          </td>
                          <td style={{ textAlign: 'center', fontWeight: 'bold' }}>
                            {statusChanged ? (
                              <span style={{ color: task.status_2 === 'passed' ? 'var(--green-color)' : 'var(--red-color)' }}>
                                {task.status_1 === 'passed' ? '▼ LOST' : '▲ GAINED'}
                              </span>
                            ) : (
                              <span style={{ color: 'var(--text-dimmed)', fontFamily: 'var(--font-mono)' }}>=</span>
                            )}
                          </td>
                          <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>{task.tokens_1?.toLocaleString() || '-'}</td>
                          <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>{task.tokens_2?.toLocaleString() || '-'}</td>
                          <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontWeight: 'bold', color: tokenDiff > 0 ? 'var(--red-color)' : tokenDiff < 0 ? 'var(--green-color)' : 'inherit' }}>
                            {tokenDiff > 0 ? `+${tokenDiff.toLocaleString()}` : tokenDiff < 0 ? `${tokenDiff.toLocaleString()}` : '0'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
