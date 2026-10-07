// Copyright (c) 2026 Huawei Technologies Co., Ltd.
// All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Resolve a registry address without discarding the HTTPS setting. */
export const resolveBaseUrl = (cfg, { development = false, pageProtocol = 'http:' } = {}) => {
    if (development || !cfg) return ''
    if (cfg.mode === 'nginx') {
        const value = (cfg.nginxUrl || '').trim()
        if (!value) return ''
        // Same-origin paths ("/api/registry-center") and protocol-relative URLs
        // ("//host/path") inherit the page scheme, so they neither downgrade nor
        // need parsing. The Settings UI ships the path form as its own example.
        if (value.startsWith('//') || (value.startsWith('/') && !value.includes('://'))) {
            return value.replace(/\/+$/, '')
        }
        if (!/^https?:\/\//i.test(value)) {
            throw new Error(
                'Registry URL must use HTTP(S): give an absolute URL or a path starting with /')
        }
        const url = new URL(value)
        if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
            throw new Error('Registry URL must use HTTP(S) without embedded credentials')
        }
        if (pageProtocol === 'https:' && url.protocol !== 'https:') {
            throw new Error('An HTTPS page requires an HTTPS registry URL')
        }
        return value.replace(/\/+$/, '')
    }
    const protocol = cfg.https === true || pageProtocol === 'https:' ? 'https:' : 'http:'
    const host = cfg.ip || '127.0.0.1'
    const authority = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host
    return `${protocol}//${authority}:${cfg.port || '5000'}`
}
