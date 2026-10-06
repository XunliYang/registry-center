// Copyright (c) 2026 Huawei Technologies Co., Ltd.
// All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import test from 'node:test'
import assert from 'node:assert/strict'
import { resolveBaseUrl } from '../src/service/base_url.js'

test('development and an unset address use the same origin', () => {
    assert.equal(resolveBaseUrl({ https: true }, { development: true }), '')
    assert.equal(resolveBaseUrl(null, { pageProtocol: 'https:' }), '')
})
test('saved HTTPS selection reaches the HTTPS service', () => {
    assert.equal(resolveBaseUrl({ ip: '127.0.0.1', port: '5000', https: true }), 'https://127.0.0.1:5000')
})
test('an HTTPS page does not downgrade IP mode to HTTP', () => {
    assert.equal(resolveBaseUrl({ ip: 'registry.example', https: false }, { pageProtocol: 'https:' }),
        'https://registry.example:5000')
})
test('HTTP remains available for an explicit local HTTP deployment', () => {
    assert.equal(resolveBaseUrl({ https: false }), 'http://127.0.0.1:5000')
})
test('IPv6 addresses have brackets', () => {
    assert.equal(resolveBaseUrl({ ip: '::1', https: true }), 'https://[::1]:5000')
})
test('proxy URLs retain their configured path without duplicate trailing slash', () => {
    assert.equal(resolveBaseUrl({ mode: 'nginx', nginxUrl: 'https://registry.example/service/' }),
        'https://registry.example/service')
})
test('a same-origin gateway path keeps working on an HTTPS page', () => {
    // This is the Settings UI's own placeholder and i18n example.
    assert.equal(resolveBaseUrl({ mode: 'nginx', nginxUrl: '/api/registry-center' },
        { pageProtocol: 'https:' }), '/api/registry-center')
    assert.equal(resolveBaseUrl({ mode: 'nginx', nginxUrl: '/api/registry-center/' }),
        '/api/registry-center')
    assert.equal(resolveBaseUrl({ mode: 'nginx', nginxUrl: '   ' }), '')
})
test('a protocol-relative gateway URL inherits the page scheme', () => {
    assert.equal(resolveBaseUrl({ mode: 'nginx', nginxUrl: '//registry.example/api' },
        { pageProtocol: 'https:' }), '//registry.example/api')
})
test('a gateway value without a scheme is rejected instead of silently rewritten', () => {
    assert.throws(() => resolveBaseUrl({ mode: 'nginx', nginxUrl: 'registry.example:5000' }), /HTTP/)
})
test('insecure proxy URLs on HTTPS pages and non-HTTP schemes are rejected', () => {
    assert.throws(() => resolveBaseUrl({ mode: 'nginx', nginxUrl: 'http://registry.example' },
        { pageProtocol: 'https:' }), /HTTPS/)
    assert.throws(() => resolveBaseUrl({ mode: 'nginx', nginxUrl: 'javascript:alert(1)' }), /HTTP/)
})
