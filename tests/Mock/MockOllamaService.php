<?php

namespace App\Tests\Mock;

use App\Service\OllamaServiceInterface;

class MockOllamaService implements OllamaServiceInterface
{
    private string $returnedText = '';
    private array $availableModels = [];
    private bool $throwOnGetModels = false;

    public function setReturnedText(string $text): void
    {
        $this->returnedText = $text;
    }

    public function setAvailableModels(array $models): void
    {
        $this->availableModels = $models;
    }

    public function setShouldThrowOnGetModels(bool $throw): void
    {
        $this->throwOnGetModels = $throw;
    }

    public function generateText(string $model, string $prompt): string
    {
        return $this->returnedText;
    }

    public function getAvailableModels(): array
    {
        if ($this->throwOnGetModels) {
            throw new \RuntimeException('Ollama service unreachable');
        }

        return $this->availableModels;
    }

    public function reset(): void
    {
        $this->returnedText = '';
        $this->availableModels = [];
        $this->throwOnGetModels = false;
    }
}
