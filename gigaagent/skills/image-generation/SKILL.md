---
name: image-generation
description: Draw a picture with Kandinsky — how to write the query, what a style name does, and how a drawing can fail. Use when the user asks for an image, an illustration, a picture or a drawing.
version: 2.0.0
type: instruction
when_to_use: The user asks to draw, illustrate or generate a picture; Russian equivalents include нарисуй, сгенерируй картинку, изображение, иллюстрация, картинку в стиле.
---

# Image generation

`image_generate` draws one picture and delivers it to the user itself — there is no file
to send afterwards and nothing is stored on the drive. Enable it with
`enable_tools(tools='image_generate')`, then call it once per picture; several variants
mean several calls.

```
image_generate(query="рыжий кот в скафандре на фоне Сатурна")
```

An `OK` answer means the picture is already in the chat. Do not describe it back, do not
call the tool again to make sure, and do not try to send it with another tool.

## Writing the query

Describe the picture, not a request to a chatbot. Build it in layers, comma-separated:
the subject, what it is doing, the setting and time of day, the look, the light and colors,
the framing. «Ёж в вязаном шарфе на самокате, горная тропа на закате, розово-золотые облака,
мягкий тёплый свет, средний план» draws better than «нарисуй ежа».

Say what should be there rather than what should not: «чистый фон, резкий фокус» works more
reliably than «без размытия». Size, orientation and model version are not parameters — the
picture comes back in whatever shape the service draws, so ask for a vertical or horizontal
frame inside the query itself and treat it as a request rather than a setting.

## Styles

`style` names a look the service knows, such as `picasso`. A name it does not know is accepted
and then ignored: the picture comes back drawn without any style and nothing says so. Never
promise the user a look that only lives in the `style` argument — describe it inside `query`
as well, «в стиле акварели, мягкие переходы», where the words cannot be dropped silently.

## Limits and failures

Drawing runs for up to a few minutes: one call, one wait, no progress to report meanwhile.

The service runs its own censorship over what it draws, and it decides — the tool passes the
prompt through unchanged. A prompt it rejects comes back as a tool error naming the censor;
report that refusal instead of rewording the prompt to get around it.

A `TOOL_ERROR` saying the pipeline is busy means the queue is full right now, not that image
generation is unavailable: say so and offer to try again, and do not retry in a loop. A
`TOOL_ERROR` naming a `FUSIONBRAIN_*` setting means the deployment is not configured for image
generation at all — say so instead of retrying.
