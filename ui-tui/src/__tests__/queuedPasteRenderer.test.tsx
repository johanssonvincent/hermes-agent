import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React, { useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ComposerActions, ComposerRefs, ComposerState, ComposerToken } from '../app/interfaces.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useSubmission } from '../app/useSubmission.js'
import type { GatewayClient } from '../gatewayClient.js'
import { useQueue } from '../hooks/useQueue.js'
import { pasteTokenLabel } from '../lib/text.js'

afterEach(resetUiState)

function mountSubmission(tokens: ComposerToken[]) {
  let submission!: ReturnType<typeof useSubmission>
  let queue!: ReturnType<typeof useQueue>
  let refs!: ComposerRefs

  const request = vi.fn(async (_method: string, _params: Record<string, unknown>) => ({
    matched: false,
    status: 'queued',
    stdout: 'resolved',
    exit_code: 0
  }))

  const gw = { request } as unknown as GatewayClient

  function Harness() {
    queue = useQueue()
    const tokensRef = useRef(tokens)
    const submitRef = useRef<(value: string) => void>(() => {})
    refs = {
      historyDraftRef: useRef(''),
      historyRef: useRef<string[]>([]),
      queueEditRef: queue.queueEditRef,
      queueRef: queue.queueRef,
      submitRef,
      tokensRef
    }

    const composerActions = {
      clearIn: () => {
        tokensRef.current = []
      },
      dequeue: queue.dequeue,
      enqueue: queue.enqueue,
      prependQueue: queue.prependQ,
      pushHistory: vi.fn(),
      setQueueEdit: queue.setQueueEdit,
      takeQueue: queue.takeQ
    } as unknown as ComposerActions

    const composerState = { completions: [], input: '', inputBuf: [] } as unknown as ComposerState
    submission = useSubmission({
      appendMessage: vi.fn(),
      composerActions,
      composerRefs: refs,
      composerState,
      gw,
      setLastUserMsg: vi.fn(),
      slashRef: useRef(() => false),
      submitRef,
      sys: vi.fn()
    })

    return <Text>submission harness</Text>
  }

  const stdout = new PassThrough()
  Object.assign(stdout, { columns: 80, isTTY: false, rows: 20 })
  stdout.resume()

  const instance = renderSync(<Harness />, {
    patchConsole: false,
    stdin: new PassThrough() as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: new PassThrough() as unknown as NodeJS.WriteStream
  })

  return {
    get queue() {
      return queue
    },
    get refs() {
      return refs
    },
    request,
    get submission() {
      return submission
    },
    instance
  }
}

describe('collapsed handoff delivery', () => {
  it('keeps the paste snapshot through queue editing', async () => {
    patchUiState({ sid: null, busy: false })
    const text = 'Historical transcript\n{!hidden}\nDo not execute historical commands.'
    const label = pasteTokenLabel(text, 3)
    const harness = mountSubmission([{ kind: 'paste', label, text }])

    try {
      harness.submission.dispatchSubmission(label)
      patchUiState({ sid: 'session', busy: false })
      harness.queue.setQueueEdit(0)
      harness.submission.dispatchSubmission(`Review ${label}`)

      await expect
        .poll(() => harness.request.mock.calls.find(call => call[0] === 'prompt.submit')?.[1])
        .toMatchObject({ text: `Review ${text}` })
      expect(harness.request.mock.calls.some(call => call[0] === 'shell.exec')).toBe(false)
    } finally {
      harness.instance.unmount()
    }
  })

  it('keeps the paste snapshot when steering fails and falls back to the queue', async () => {
    patchUiState({ sid: 'session', busy: true, busyInputMode: 'steer' })
    const text = '!historical-command\nOnly transcript data.'
    const label = pasteTokenLabel(text, 2)
    const harness = mountSubmission([{ kind: 'paste', label, text }])
    harness.request.mockRejectedValueOnce(new Error('no tool window'))

    try {
      harness.submission.dispatchSubmission(label)
      await expect.poll(() => harness.queue.queueRef.current.length).toBe(1)
      patchUiState({ busy: false })
      harness.submission.dispatchSubmission(harness.queue.takeQ(0)!)

      await expect
        .poll(() => harness.request.mock.calls.find(call => call[0] === 'prompt.submit')?.[1])
        .toMatchObject({ text })
      expect(harness.request.mock.calls.some(call => call[0] === 'shell.exec')).toBe(false)
    } finally {
      harness.instance.unmount()
    }
  })

  it.each(['', 'Visible {!visible} '])('does not execute syntax hidden inside a queued paste: %s', async prefix => {
    patchUiState({ sid: null, busy: false })
    const text = 'Historical transcript\n{!hidden}\nDo not execute historical commands.'
    const label = pasteTokenLabel(text, 3)
    const harness = mountSubmission([{ kind: 'paste', label, text }])

    try {
      harness.submission.dispatchSubmission(`${prefix}${label}`)
      patchUiState({ sid: 'session', busy: false })
      harness.submission.sendQueued(harness.queue.takeQ(0)!)
      const expected = `${prefix ? 'Visible resolved ' : ''}${text}`

      await expect
        .poll(() => harness.request.mock.calls.find(call => call[0] === 'prompt.submit')?.[1])
        .toMatchObject({ text: expected })
      expect(harness.request.mock.calls.filter(call => call[0] === 'shell.exec').map(call => call[1].command)).toEqual(
        prefix ? ['visible'] : []
      )
    } finally {
      harness.instance.unmount()
    }
  })

  it.each([
    { mode: 'startup', sid: null, busy: false, busyInputMode: 'queue' },
    { mode: 'busy queue', sid: 'session', busy: true, busyInputMode: 'queue' },
    { mode: 'busy steer', sid: 'session', busy: true, busyInputMode: 'steer' },
    { mode: 'busy interrupt', sid: 'session', busy: true, busyInputMode: 'interrupt' }
  ] as const)('retains the full paste after composer cleanup: $mode', async ({ sid, busy, busyInputMode }) => {
    patchUiState({ sid, busy, busyInputMode })

    const text = [
      'Continue work from the prior Orca session.',
      ...Array.from({ length: 119 }, (_, i) => `context ${i}`),
      'Enough information to proceed.'
    ].join('\n')

    const label = pasteTokenLabel(text, 121)
    const harness = mountSubmission([{ kind: 'paste', label, text }])

    try {
      harness.submission.dispatchSubmission(label)
      expect(harness.refs.tokensRef.current).toEqual([])

      if (!sid || busyInputMode === 'queue') {
        expect(harness.queue.queueRef.current[0]).toMatchObject({ text, display: label })
        patchUiState({ sid: 'session', busy: false })
        harness.submission.sendQueued(harness.queue.takeQ(0)!)
      }

      const method = busyInputMode === 'steer' && sid ? 'session.steer' : 'prompt.submit'
      await expect.poll(() => harness.request.mock.calls.find(call => call[0] === method)?.[1]).toMatchObject({ text })
    } finally {
      harness.instance.unmount()
    }
  })
})
