<?php

namespace App\Controller;

use App\Entity\File;
use App\Entity\User;
use App\Service\FileStorageServiceInterface;
use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\Routing\Attribute\Route;
use Symfony\Component\Security\Http\Attribute\CurrentUser;
use Symfony\Component\Security\Http\Attribute\IsGranted;

#[IsGranted('IS_AUTHENTICATED_FULLY')]
#[Route('/api', name: 'app_api_text')]
class TextController extends AbstractController
{
    public function __construct(private readonly FileStorageServiceInterface $fileStorage)
    {
    }

    #[Route('/file/{id}/contents', name: 'file_contents', methods: ['GET'])]
    public function fileContents(#[CurrentUser] User $user, File $file): Response
    {
        if ($file->getUser() !== $user) {
            return $this->json(['message' => 'You do not have access to this file'], Response::HTTP_FORBIDDEN);
        }

        $path = $file->getPath();

        if (!$this->fileStorage->exists($path)) {
            return $this->json(['message' => 'File not found on disk'], Response::HTTP_NOT_FOUND);
        }

        return new Response($this->fileStorage->read($path), Response::HTTP_OK, ['Content-Type' => 'text/plain']);
    }

    #[Route('/file/{id}/contents', name: 'update_file_contents', methods: ['POST'])]
    public function updateFileContents(#[CurrentUser] User $user, File $file, Request $request): Response
    {
        if ($file->getUser() !== $user) {
            return $this->json(['message' => 'You do not have access to this file'], Response::HTTP_FORBIDDEN);
        }

        $path = $file->getPath();

        if (!$this->fileStorage->exists($path)) {
            return $this->json(['message' => 'File not found on disk'], Response::HTTP_NOT_FOUND);
        }

        $this->fileStorage->write($path, $request->getContent());

        return new Response(null, Response::HTTP_NO_CONTENT);
    }
}
