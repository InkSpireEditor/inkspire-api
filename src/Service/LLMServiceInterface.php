<?php

namespace App\Service;

interface LLMServiceInterface
{
    /**
     * Generates text using the specified model and prompt.
     * The model must be prefixed with the provider name: "provider/model".
     *
     * @param string $model  Prefixed model identifier, e.g. "local-ollama/llama3".
     * @param string $prompt The user's prompt.
     * @return string The generated text.
     */
    public function generateText(string $model, string $prompt): string;

    /**
     * Returns the list of available models across all configured providers.
     * Each entry has a "name" key containing the prefixed model identifier.
     *
     * @return array<int, array{name: string}>
     */
    public function getAvailableModels(): array;
}
