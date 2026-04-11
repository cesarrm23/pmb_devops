/**
 * Playwright E2E: Git auth for admin vs non-admin users
 */
import { chromium } from 'playwright';

const BASE = 'http://localhost:8075';
const SPA = `${BASE}/web#action=780`;
const ADMIN = { login: 'cesarrm23@gmail.com', password: 'cincinati930621' };
const TESTER = { login: 'devtest@pmb.test', password: 'devtest123' };

let browser;
const results = [];

function log(test, ok, detail = '') {
    console.log(`[${ok ? 'PASS' : 'FAIL'}] ${test}${detail ? ': ' + detail : ''}`);
    results.push({ test, ok, detail });
}

async function newSession(creds) {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    await page.request.post(`${BASE}/web/session/authenticate`, {
        data: { jsonrpc: '2.0', method: 'call', params: { db: 'odooal', ...creds } },
    });
    return page;
}

async function goAI(page) {
    await page.goto(SPA, { waitUntil: 'load', timeout: 30000 });
    await page.waitForTimeout(5000);
    // Select an instance first (production is usually pre-selected for admin)
    const prod = page.getByText('production', { exact: true }).first();
    if (await prod.count() > 0) {
        await prod.click();
        await page.waitForTimeout(2000);
    }
    // Now click AI tab
    const aiTab = page.locator('text=✨ AI').first();
    if (await aiTab.count() === 0) {
        // Fallback: try without emoji
        await page.getByText('AI').first().click();
    } else {
        await aiTab.click();
    }
    await page.waitForTimeout(4000);
}

// =========================================================================
// TEST 1: Admin sees commit buttons directly (no login form)
// =========================================================================
async function testAdmin() {
    console.log('\n=== ADMIN USER ===');
    const page = await newSession(ADMIN);
    await goAI(page);

    const text = await page.locator('body').innerText();

    // Should NOT see auth login form
    const hasAuthForm = text.includes('Autenticación para commit/push');
    log('Admin: no auth form', !hasAuthForm, hasAuthForm ? 'auth form visible but shouldnt be' : '');

    // Should see commit buttons (if there are changes)
    const hasCommitBtn = text.includes('Commit');
    const hasNoChanges = text.includes('No hay cambios');
    log('Admin: commit area visible', hasCommitBtn || hasNoChanges,
        hasCommitBtn ? 'commit button found' : hasNoChanges ? 'no changes (ok)' : 'neither found');

    // Should see git icon when collapsed
    await page.getByText('Git Changes').first().click();
    await page.waitForTimeout(500);
    const collapsed = await page.locator('svg circle').count();
    log('Admin: collapsed shows git icon', collapsed > 0, `${collapsed} svg circles`);

    // Re-expand
    await page.locator('.pmb-git-panel').first().click();
    await page.waitForTimeout(500);

    await page.screenshot({ path: '/tmp/pmb_auth_admin.png' });
    await page.close();
}

// =========================================================================
// TEST 2: Non-admin sees login form, can authenticate
// =========================================================================
async function testNonAdmin() {
    console.log('\n=== NON-ADMIN USER ===');
    const page = await newSession(TESTER);
    await goAI(page);

    const text = await page.locator('body').innerText();
    await page.screenshot({ path: '/tmp/pmb_auth_nonadmin.png' });

    // Should see auth login form
    const hasAuthForm = text.includes('Autenticación para commit/push');
    log('Non-admin: auth form visible', hasAuthForm);

    // Should NOT see commit buttons
    const commitIdx = text.indexOf('Commit');
    const authIdx = text.indexOf('Autenticación');
    // Commit button should not exist, or only in the auth context
    const hasCommitAction = text.includes('Commit + Push');
    log('Non-admin: commit buttons hidden', !hasCommitAction,
        hasCommitAction ? 'commit buttons visible but shouldnt be' : '');

    // Should still see file changes (read-only)
    const hasRepoSelector = text.includes('odoo') || text.includes('pmb_devops');
    log('Non-admin: can see repos', hasRepoSelector);

    // Test wrong password
    await page.fill('input[placeholder="Usuario (login)"]', TESTER.login);
    await page.fill('input[placeholder="Contraseña"]', 'wrongpassword');
    await page.locator('button:has-text("Iniciar sesión")').click();
    await page.waitForTimeout(2000);

    const errText = await page.locator('body').innerText();
    const hasError = errText.includes('incorrecta') || errText.includes('no encontrado');
    log('Non-admin: wrong password shows error', hasError);
    await page.screenshot({ path: '/tmp/pmb_auth_wrong.png' });

    // Test correct password
    await page.fill('input[placeholder="Contraseña"]', TESTER.password);
    await page.locator('button:has-text("Iniciar sesión")').click();
    await page.waitForTimeout(2000);

    const afterAuth = await page.locator('body').innerText();
    const authFormGone = !afterAuth.includes('Autenticación para commit/push');
    log('Non-admin: auth form disappears after login', authFormGone);

    const hasCommitNow = afterAuth.includes('Commit') || afterAuth.includes('No hay cambios');
    log('Non-admin: commit area visible after auth', hasCommitNow);
    await page.screenshot({ path: '/tmp/pmb_auth_success.png' });

    await page.close();
}

// =========================================================================
// TEST 3: Backend rejects non-authed commit/push
// =========================================================================
async function testBackendGuard() {
    console.log('\n=== BACKEND GUARD ===');
    const page = await newSession(TESTER);

    // Call commit endpoint directly without git auth
    const resp = await page.request.post(`${BASE}/devops/git/commit`, {
        data: { jsonrpc: '2.0', method: 'call', params: { project_id: 1, repo_path: '/tmp', message: 'test' } },
    });
    const body = await resp.json();
    const result = body.result || {};
    log('Backend: commit blocked without auth', result.auth_required === true,
        JSON.stringify(result).substring(0, 100));

    // Same for push
    const resp2 = await page.request.post(`${BASE}/devops/git/push`, {
        data: { jsonrpc: '2.0', method: 'call', params: { project_id: 1, repo_path: '/tmp' } },
    });
    const body2 = await resp2.json();
    const result2 = body2.result || {};
    log('Backend: push blocked without auth', result2.auth_required === true);

    await page.close();
}

// =========================================================================
async function main() {
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
    try {
        await testAdmin();
        await testNonAdmin();
        await testBackendGuard();
    } catch (e) {
        console.error('FATAL:', e.message);
    } finally {
        console.log('\n========== SUMMARY ==========');
        const pass = results.filter(r => r.ok).length;
        const fail = results.filter(r => !r.ok).length;
        console.log(`Total: ${results.length} | PASS: ${pass} | FAIL: ${fail}`);
        if (fail > 0) {
            console.log('\nFailed:');
            results.filter(r => !r.ok).forEach(r =>
                console.log(`  [FAIL] ${r.test}${r.detail ? ': ' + r.detail : ''}`)
            );
        }
        await browser.close();
        process.exit(fail > 0 ? 1 : 0);
    }
}

main();
