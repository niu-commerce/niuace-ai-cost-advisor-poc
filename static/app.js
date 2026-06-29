const state = {
  data: null,
  contracts: [],
  selectedLetterAwardId: null,
  selectedItemId: 'waterproofing',
};

const els = {
  landing: document.getElementById('landingPage'),
  dashboard: document.getElementById('dashboardPage'),
  start: document.getElementById('startAnalysisBtn'),
  back: document.getElementById('backBtn'),
  viewRisk: document.getElementById('viewRiskBtn'),
  kpiGrid: document.getElementById('kpiGrid'),
  riskBody: document.getElementById('riskTableBody'),
  detail: document.getElementById('detailPanel'),
  chat: document.getElementById('chatPanel'),
  recommendation: document.getElementById('recommendationPanel'),
  riskSection: document.getElementById('riskSection'),
  contractPickerWrap: document.getElementById('contractPickerWrap'),
  contractSelect: document.getElementById('contractSelect'),
  contractNameInput: document.getElementById('contractNameInput'),
  contractorInput: document.getElementById('contractorInput'),
  contractValueInput: document.getElementById('contractValueInput'),
  bqItemsInput: document.getElementById('bqItemsInput'),
  dashboardSubtitle: document.getElementById('dashboardSubtitle'),
  assessmentBadge: document.getElementById('assessmentBadge'),
  assessmentMessage: document.getElementById('assessmentMessage'),
  riskCountPill: document.getElementById('riskCountPill'),
};

function moneyClass(risk) {
  return String(risk).toLowerCase();
}

function renderKpis(kpis) {
  els.kpiGrid.innerHTML = kpis.map(kpi => `
    <article class="kpi-card ${kpi.tone}">
      <span>${kpi.label}</span>
      <strong>${kpi.value}</strong>
    </article>
  `).join('');
}

function renderRiskTable(items) {
  if (!items.length) {
    els.riskBody.innerHTML = `
      <tr>
        <td colspan="8" class="empty-row">No abnormal BQ items found for this contract.</td>
      </tr>
    `;
    return;
  }

  els.riskBody.innerHTML = items.map(item => `
    <tr>
      <td><strong>${item.bq_item}</strong></td>
      <td>${item.unit}</td>
      <td>${item.current_rate}</td>
      <td>${item.historical_average}</td>
      <td>${item.difference}</td>
      <td><span class="risk-badge ${moneyClass(item.risk_level)}">${item.risk_level}</span></td>
      <td>${item.recommendation}</td>
      <td><button class="row-action" data-item-id="${item.id}">View Details</button></td>
    </tr>
  `).join('');

  document.querySelectorAll('[data-item-id]').forEach((button) => {
    button.addEventListener('click', () => {
      state.selectedItemId = button.getAttribute('data-item-id');
      renderDetailPanel();
      els.detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
}

function renderDetailPanel() {
  const details = state.data.item_details || {};
  const firstDetail = Object.values(details)[0];
  const detail = details[state.selectedItemId] || details.waterproofing || firstDetail;

  if (!detail) {
    els.detail.innerHTML = `
      <div class="panel-header">
        <div>
          <span class="eyebrow">BQ item benchmark</span>
          <h2>No Detail Available</h2>
        </div>
      </div>
      <div class="ai-note">No priced BQ item benchmark detail is available for this contract.</div>
    `;
    return;
  }

  els.detail.innerHTML = `
    <div class="panel-header">
      <div>
        <span class="eyebrow">BQ item benchmark</span>
        <h2>${detail.title}</h2>
      </div>
      <span class="risk-badge medium">Medium</span>
    </div>

    <div class="detail-grid">
      <div class="metric-box"><span>Current Rate</span><strong>${detail.current_rate}</strong></div>
      <div class="metric-box"><span>Historical Average</span><strong>${detail.historical_average}</strong></div>
      <div class="metric-box"><span>Difference</span><strong>${detail.difference}</strong></div>
      <div class="metric-box"><span>Lowest Historical Rate</span><strong>${detail.lowest_historical_rate}</strong></div>
      <div class="metric-box"><span>Highest Historical Rate</span><strong>${detail.highest_historical_rate}</strong></div>
      <div class="metric-box"><span>Historical Records Found</span><strong>${detail.historical_records_found}</strong></div>
    </div>

    <div class="ai-note">${detail.ai_explanation}</div>

    <h3>Similar Projects</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Project</th><th>Contractor</th><th>Rate</th><th>Similarity</th></tr></thead>
        <tbody>
          ${detail.similar_projects.map(row => `
            <tr><td>${row.project}</td><td>${row.contractor}</td><td>${row.rate}</td><td>${row.similarity}</td></tr>
          `).join('')}
        </tbody>
      </table>
    </div>

    <h3>Missing Evidence Checklist</h3>
    <ul class="checklist">
      ${detail.evidence.map(row => `
        <li><span>${row.label}</span><strong>${row.status}</strong></li>
      `).join('')}
    </ul>
  `;
}

async function askQuestion(question) {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, item_id: state.selectedItemId }),
  });
  const data = await response.json();
  renderChatPanel(question, data.answer);
}

function renderChatPanel(question = 'Why is waterproofing expensive?', answer = null) {
  const aiAnswer = answer || 'Possible reasons include imported material, new specification, smaller quantity, or complex detailing. However, consultant justification and supplier quotation are missing. Recommendation: request supporting documents before approval.';
  els.chat.innerHTML = `
    <div class="panel-header">
      <div>
        <span class="eyebrow">Cost advisor</span>
        <h2>Ask NiuAce AI</h2>
      </div>
    </div>
    <div class="chat-log">
      <div class="bubble user">${question}</div>
      <div class="bubble ai">${aiAnswer}</div>
    </div>
    <div class="suggestions">
      ${['Show similar projects', 'Is this rate reasonable?', 'What is the cost impact?', 'Can approve or not?'].map(q => `
        <button data-question="${q}">${q}</button>
      `).join('')}
    </div>
  `;

  els.chat.querySelectorAll('[data-question]').forEach((button) => {
    button.addEventListener('click', () => askQuestion(button.getAttribute('data-question')));
  });
}

function renderRecommendationCard(recommendation) {
  els.recommendation.innerHTML = `
    <div class="panel-header">
      <div>
        <span class="eyebrow">Final recommendation</span>
        <h2>Management Decision</h2>
      </div>
      <span class="risk-badge medium">${recommendation.risk_level}</span>
    </div>
    <div class="recommendation-list">
      <div><span>Reason</span><strong>${recommendation.reason}</strong></div>
      <div><span>Estimated Cost Impact</span><strong>${recommendation.estimated_cost_impact}</strong></div>
      <div><span>Recommendation</span><strong>${recommendation.recommendation}</strong></div>
      <div><span>Confidence</span><strong>${recommendation.confidence}</strong></div>
    </div>
  `;
}

function formatMoney(value) {
  const amount = Number(value || 0);
  if (!amount) return '-';
  return `RM${amount.toLocaleString('en-MY', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function updateLandingPreview(contract) {
  if (!contract) return;
  els.contractNameInput.value = contract.contract_no || '-';
  els.contractorInput.value = contract.contractor || '-';
  els.contractValueInput.value = formatMoney(contract.total_contract_amount);
  els.bqItemsInput.value = Number(contract.total_bq_items || 0).toLocaleString('en-MY');
}

async function loadContracts() {
  try {
    const response = await fetch('/api/contracts?company_id=1452&limit=50');
    const data = await response.json();
    state.contracts = data.rows || [];

    if (!state.contracts.length) return;

    els.contractPickerWrap.classList.remove('hidden');
    els.contractSelect.innerHTML = state.contracts.map((contract) => `
      <option value="${contract.letter_award_id}">
        ${contract.contract_no} | ${contract.business_unit || '-'} | ${contract.category_of_work || '-'}
      </option>
    `).join('');

    state.selectedLetterAwardId = els.contractSelect.value;
    updateLandingPreview(state.contracts[0]);
  } catch (error) {
    console.warn('Unable to load real contract list', error);
  }
}

function renderDashboardHeader() {
  const contract = state.data.contract || {};
  els.dashboardSubtitle.textContent = `${contract.name || '-'} - ${contract.contractor || '-'}`;
  els.assessmentBadge.textContent = state.data.assessment?.badge || 'Normal';
  els.assessmentMessage.textContent = state.data.assessment?.message || '';

  const riskCount = state.data.high_risk_items?.length || 0;
  els.viewRisk.textContent = `View ${riskCount} High Risk Items`;
  els.riskCountPill.textContent = `${riskCount} abnormal items`;
}

async function startAnalysis() {
  const params = new URLSearchParams();
  if (state.selectedLetterAwardId) {
    params.set('letter_award_id', state.selectedLetterAwardId);
  }
  const response = await fetch(`/api/analysis${params.toString() ? `?${params}` : ''}`);
  state.data = await response.json();
  state.selectedItemId = state.data.high_risk_items[0]?.id || Object.keys(state.data.item_details || {})[0] || 'waterproofing';
  renderDashboardHeader();
  renderKpis(state.data.kpis);
  renderRiskTable(state.data.high_risk_items);
  renderDetailPanel();
  renderChatPanel();
  renderRecommendationCard(state.data.recommendation);
  els.landing.classList.add('hidden');
  els.dashboard.classList.remove('hidden');
}

els.start.addEventListener('click', startAnalysis);
els.contractSelect.addEventListener('change', () => {
  state.selectedLetterAwardId = els.contractSelect.value;
  const selected = state.contracts.find((contract) => String(contract.letter_award_id) === state.selectedLetterAwardId);
  updateLandingPreview(selected);
});
els.back.addEventListener('click', () => {
  els.dashboard.classList.add('hidden');
  els.landing.classList.remove('hidden');
});
els.viewRisk.addEventListener('click', () => {
  els.riskSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
});

loadContracts();
