/**
 * Playwright E2E test for pmb_devops SPA
 */
import { chromium } from 'playwright';

const BASE = 'http://localhost:8075';
const LOGIN = 'cesarrm23@gmail.com';
const PASSWORD = 'cincinati930621';
const SPA_URL = `${BASE}/web#action=780`;

let browser, page;
const results = [];

function log(tab, test, ok, detail = '') {
    const s = ok ? 'PASS' : 'FAIL';
    console.log(`[${s}] ${tab} — ${test}${detail ? ': ' + detail : ''}`);
    results.push({ tab, test, ok, detail });
}

async function setup() {
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
    page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    await page.request.post(`${BASE}/web/session/authenticate`, {
        data: { jsonrpc: '2.0', method: 'call', params: { db: 'odooal', login: LOGIN, password: PASSWORD } },
    });
    console.log('Logged in\n');
}

async function goSPA() {
    await page.goto(SPA_URL, { waitUntil: 'load', timeout: 30000 });
    await page.waitForTimeout(5000);
}

async function selectProject(name) {
    const sel = page.locator('select').first();
    const opts = await sel.locator('option').allTextContents();
    const match = opts.find(o => o.includes(name));
    if (!match) return false;
    await sel.selectOption({ label: match });
    await page.waitForTimeout(3000);
    return true;
}

async function selectInstance(name) {
    const el = page.getByText(name, { exact: true }).first();
    if (await el.count() > 0) { await el.click(); await page.waitForTimeout(3000); return true; }
    return false;
}

async function clickTab(name) {
    const tab = page.getByText(name).first();
    if (await tab.count() > 0) { await tab.click(); await page.waitForTimeout(3000); return true; }
    return false;
}

async function ss(name) {
    await page.screenshot({ path: `/tmp/pmb_${name}.png`, fullPage: true });
}

async function bodyText() {
    return page.locator('body').innerText();
}

// =========================================================================
// HISTORY: check repo selector shows expected repos
// =========================================================================
async function testHistory(project, instance, expectedRepos) {
    const L = `${project}/${instance}`;
    await goSPA();
    if (!await selectProject(project)) { log('HISTORY', `${L} project`, false, 'not found'); return; }
    if (!await selectInstance(instance)) { log('HISTORY', `${L} instance`, false, 'not found'); return; }
    await clickTab('HISTORY');
    await ss(`history_${project.replace(/\s/g, '')}_${instance}`);
    const text = await bodyText();
    for (const r of expectedRepos) {
        log('HISTORY', `${L} — "${r}"`, text.includes(r), '');
    }
}

// =========================================================================
// UPGRADE: check deploy cards show custom repos, not system repos
// =========================================================================
async function testUpgrade(project, instance, expectedCustom, notExpected) {
    const L = `${project}/${instance}`;
    await goSPA();
    if (!await selectProject(project)) { log('UPGRADE', `${L} project`, false, 'not found'); return; }
    if (!await selectInstance(instance)) { log('UPGRADE', `${L} instance`, false, 'not found'); return; }
    await clickTab('UPGRADE');
    await ss(`upgrade_${project.replace(/\s/g, '')}_${instance}`);
    const text = await bodyText();

    // Check deploy section exists
    const hasSection = text.includes('Desplegar') || text.includes('Pull');
    log('UPGRADE', `${L} — deploy section`, hasSection, '');

    for (const r of expectedCustom) {
        log('UPGRADE', `${L} — "${r}" visible`, text.includes(r), '');
    }
    // Check system repos not in deploy area
    const deployStart = text.indexOf('Desplegar cambios');
    const deployEnd = text.indexOf('Deploy result') > 0 ? text.indexOf('Deploy result') : text.length;
    const deploySection = deployStart >= 0 ? text.substring(deployStart, deployEnd) : text;
    for (const r of (notExpected || [])) {
        const found = deploySection.includes(r);
        log('UPGRADE', `${L} — "${r}" NOT in deploy`, !found, found ? 'LEAKED into deploy cards' : '');
    }
}

// =========================================================================
// EDITOR: check root directories from addons_path
// =========================================================================
async function testEditor(project, instance, expectedDirs) {
    const L = `${project}/${instance}`;
    await goSPA();
    if (!await selectProject(project)) { log('EDITOR', `${L} project`, false, 'not found'); return; }
    if (!await selectInstance(instance)) { log('EDITOR', `${L} instance`, false, 'not found'); return; }
    await clickTab('EDITOR');
    await page.waitForTimeout(2000);
    await ss(`editor_${project.replace(/\s/g, '')}_${instance}`);
    const text = await bodyText();

    for (const d of expectedDirs) {
        log('EDITOR', `${L} — "${d}" visible`, text.includes(d), '');
    }
}

// =========================================================================
async function main() {
    try {
        await setup();

        // ---- ASISTENTE LISTO (already selected by default) ----
        console.log('=== ASISTENTE LISTO ===');
        await testHistory('Asistente Listo', 'production', ['odoo', 'pmb_devops']);
        await testUpgrade('Asistente Listo', 'production', ['pmb_devops'], ['odoo']);
        await testEditor('Asistente Listo', 'production', ['custom_addons', 'odoo']);

        await testHistory('Asistente Listo', 'staging-1', ['pmb_devops']);
        await testUpgrade('Asistente Listo', 'staging-1', ['pmb_devops'], []);
        await testEditor('Asistente Listo', 'staging-1', ['custom_addons']);

        // ---- CREMARA ----
        console.log('\n=== CREMARA ===');
        await testHistory('Cremara', 'production', ['odoo', 'enterprise', 'cremara_addons']);
        await testUpgrade('Cremara', 'production', ['cremara_addons'], ['odoo']);
        await testEditor('Cremara', 'production', ['cremara_addons', 'enterprise']);

        await testHistory('Cremara', 'staging-1', ['cremara_addons', 'pmb_devops']);
        await testUpgrade('Cremara', 'staging-1', ['cremara_addons', 'pmb_devops'], []);
        await testEditor('Cremara', 'staging-1', ['custom_addons', 'cremara_addons']);

        await testHistory('Cremara', 'devs', ['cremara_addons', 'pmb_devops']);
        await testUpgrade('Cremara', 'devs', ['cremara_addons', 'pmb_devops'], []);

    } catch (e) {
        console.error('FATAL:', e.message);
        if (page) await ss('fatal');
    } finally {
        console.log('\n========== SUMMARY ==========');
        const pass = results.filter(r => r.ok).length;
        const fail = results.filter(r => !r.ok).length;
        console.log(`Total: ${results.length} | PASS: ${pass} | FAIL: ${fail}`);
        if (fail > 0) {
            console.log('\nFailed:');
            results.filter(r => !r.ok).forEach(r =>
                console.log(`  [FAIL] ${r.tab} — ${r.test}${r.detail ? ': ' + r.detail : ''}`)
            );
        }
        if (browser) await browser.close();
        process.exit(fail > 0 ? 1 : 0);
    }
}

main();
