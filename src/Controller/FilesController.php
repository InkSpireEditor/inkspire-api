<?php

namespace App\Controller;

use App\Entity\Dir;
use App\Entity\User;
use App\Entity\File;
use App\Repository\DirRepository;
use App\Repository\FileRepository;
use App\Service\FilePathGenerator;
use App\Service\FileStorageServiceInterface;
use Doctrine\DBAL\Exception\UniqueConstraintViolationException;
use Doctrine\ORM\EntityManagerInterface;
use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\Routing\Attribute\Route;
use Symfony\Component\Security\Http\Attribute\CurrentUser;
use Symfony\Component\Security\Http\Attribute\IsGranted;

/**
 * Controller for handling file and directory operations.
 */
#[IsGranted('IS_AUTHENTICATED_FULLY')]
#[Route('/api', name: 'app_api')]
class FilesController extends AbstractController
{
    public function __construct(
        private readonly FilePathGenerator $filePathGenerator,
        private readonly FileStorageServiceInterface $fileStorage,
    ) {
    }

    /**
     * Retrieves the file and directory tree for the authenticated user.
     *
     * @param User|null $user The current user.
     * @param Request $request The request object.
     * @return Response The JSON response.
     */
    #[Route('/tree', name: 'tree', methods: ['GET'])]
    public function getTree(#[CurrentUser] User $user, Request $request): Response
    {
        // Initialize an array to store file details.
        $resultFiles = [];
        // Get all files associated with the user.
        $files = $user->getFiles();
        foreach ($files as $file) {
            // Check if the file belongs to a directory or is stray.
            if ($file->getDir() === null) {
                // Store the name of each file in the resultFiles array.
                $resultFiles[$file->getId()] = [
                    "name" => $file->getName(),
                ];
            }
        }

        // Initialize an array to store directory details.
        $resultDirs = [];
        // Get all directories associated with the user.
        $dirs = $user->getDirs();
        foreach ($dirs as $dir) {
            // Store the name and summary of each directory in the resultDirs array.
            $resultDirs[$dir->getId()] = [
                "name" => $dir->getName(),
                "summary" => $dir->getSummary(),
            ];
        }

        // Return user details and their associated files and directories in JSON format.
        return $this->json([
            'user' => $user->getUserIdentifier(),
            'files' => $resultFiles,
            'dirs' => $resultDirs,
        ]);
    }

    /**
     * Retrieves information for a specific file.
     *
     * @param User|null $user The current user.
     * @param File $file The file entity.
     * @return Response The JSON response.
     */
    #[Route('/file/{id}', name: 'file_info', methods: ['GET'])]
    public function fileInfo(#[CurrentUser] User $user, File $file): Response
    {
        // Check if the user has access to the file.
        if ($file->getUser() !== $user) {
            return $this->json([
                'message' => 'You do not have access to this file',
            ], Response::HTTP_FORBIDDEN);
        }

        // Return the file information.
        return $this->json([
            'id' => $file->getId(),
            'name' => $file->getName(),
        ]);
    }

    /**
     * Retrieves information for a specific directory.
     *
     * @param User|null $user The current user.
     * @param Dir $dir The directory entity.
     * @return Response The JSON response.
     */
    #[Route('/dir/{id}', name: 'dir_info', methods: ['GET'])]
    public function dirInfo(#[CurrentUser] User $user, Dir $dir): Response
    {
        // Check if the user has access to the directory.
        if ($dir->getUser() !== $user) {
            return $this->json([
                'message' => 'You do not have access to this directory',
            ], Response::HTTP_FORBIDDEN);
        }

        // Retrieve files under the directory.
        $files = [];
        foreach ($dir->getFiles() as $file) {
            $files[$file->getId()] = [
                'name' => $file->getName(),
            ];
        }

        // Return the directory information.
        return $this->json([
            'id' => $dir->getId(),
            'name' => $dir->getName(),
            'summary' => $dir->getSummary(),
            'files' => $files,
        ]);
    }

    /**
     * Creates a new file.
     *
     * @param User|null $user The current user.
     * @param Request $request The request object.
     * @param FileRepository $fileRepository The file repository.
     * @param DirRepository $dirRepository The directory repository.
     * @param EntityManagerInterface $entityManager The entity manager.
     * @return Response The JSON response.
     */
    #[Route('/file', name: 'file_create', methods: ['POST'])]
    public function createFile(
        #[CurrentUser] User $user,
        Request $request,
        FileRepository $fileRepository,
        DirRepository $dirRepository,
        EntityManagerInterface $entityManager
    ): Response {
        // Decode the request content.
        $data = json_decode($request->getContent(), true);
        $name = trim($data['name'] ?? '');
        if ($name === '' || mb_strlen($name) > 255) {
            return $this->json(['message' => 'Invalid name: must be between 1 and 255 characters'], Response::HTTP_UNPROCESSABLE_ENTITY);
        }
        $dir = null;
        if (array_key_exists('dir', $data) and $data['dir'] !== null) {
            $dir = $dirRepository->findOneBy(['user' => $user, 'id' => $data['dir']]);
            if (!$dir) {
                return $this->json(['message' => 'Unauthorized directory'], Response::HTTP_FORBIDDEN);
            }
        }

        // Ensure the file name is unique.
        $originalName = $name;
        $i = 1;
        while ($fileRepository->findOneBy(['user' => $user, 'name' => $name])) {
            $name = $originalName . ' (' . $i++ . ')';
        }

        // Create and persist the new file entity.
        $file = new File();
        $file->setUser($user);
        $file->setName($name);
        $path = $this->filePathGenerator->generate($name);
        $file->setPath($path);
        if ($dir !== null) {
            $file->setDir($dir);
        }

        $entityManager->persist($file);

        try {
            $entityManager->flush();
        } catch (UniqueConstraintViolationException) {
            return $this->json(['message' => 'File name already exists'], Response::HTTP_CONFLICT);
        }

        $this->fileStorage->create($path);

        // Return the new file's information.
        return $this->json([
            'id' => $file->getId(),
            'name' => $file->getName(),
            'dir' => $dir !== null ? $file->getDir()->getId() : null,
        ], Response::HTTP_CREATED);
    }

    /**
     * Creates a new directory.
     *
     * @param User|null $user The current user.
     * @param Request $request The request object.
     * @param DirRepository $dirRepository The directory repository.
     * @param EntityManagerInterface $entityManager The entity manager.
     * @return Response The JSON response.
     */
    #[Route('/dir', name: 'dir_create', methods: ['POST'])]
    public function createDir(
        #[CurrentUser] User $user,
        Request $request,
        DirRepository $dirRepository,
        EntityManagerInterface $entityManager
    ): Response {
        // Decode the request content.
        $data = json_decode($request->getContent(), true);
        $name = trim($data['name'] ?? '');
        if ($name === '' || mb_strlen($name) > 255) {
            return $this->json(['message' => 'Invalid name: must be between 1 and 255 characters'], Response::HTTP_UNPROCESSABLE_ENTITY);
        }
        $summary = $data['summary'] ?? null;
        if ($summary !== null && mb_strlen((string) $summary) > 2000) {
            return $this->json(['message' => 'Summary too long (max 2000 characters)'], Response::HTTP_UNPROCESSABLE_ENTITY);
        }

        // Ensure the directory name is unique.
        $originalName = $name;
        $i = 1;
        while ($dirRepository->findOneBy(['user' => $user, 'name' => $name])) {
            $name = $originalName . ' (' . $i++ . ')';
        }

        // Create and persist the new directory entity.
        $dir = new Dir();
        $dir->setUser($user);
        $dir->setName($name);
        $dir->setSummary($summary);

        $entityManager->persist($dir);

        try {
            $entityManager->flush();
        } catch (UniqueConstraintViolationException) {
            return $this->json(['message' => 'Directory name already exists'], Response::HTTP_CONFLICT);
        }

        // Return the new directory's information.
        return $this->json([
            'id' => $dir->getId(),
            'name' => $dir->getName(),
            'summary' => $dir->getSummary(),
        ], Response::HTTP_CREATED);
    }

    /**
     * Updates an existing file.
     *
     * @param User|null $user The current user.
     * @param Request $request The request object.
     * @param File $file The file entity to update.
     * @param FileRepository $fileRepository The file repository.
     * @param DirRepository $dirRepository The directory repository.
     * @param EntityManagerInterface $entityManager The entity manager.
     * @return Response The JSON response.
     */
    #[Route('/file/{id}', name: 'file_update', methods: ['PUT'])]
    public function updateFile(
        #[CurrentUser] User $user,
        Request $request,
        File $file,
        FileRepository $fileRepository,
        DirRepository $dirRepository,
        EntityManagerInterface $entityManager
    ): Response {
        // Check if the user has access to the file.
        if ($file->getUser() !== $user) {
            return $this->json([
                'message' => 'You do not have access to this file',
            ], Response::HTTP_FORBIDDEN);
        }

        // Decode the request content.
        $data = json_decode($request->getContent(), true);

        // Update file properties if provided in the request.
        if (array_key_exists('name', $data)) {
            $newName = trim((string) ($data['name'] ?? ''));
            if ($newName === '' || mb_strlen($newName) > 255) {
                return $this->json(['message' => 'Invalid name: must be between 1 and 255 characters'], Response::HTTP_UNPROCESSABLE_ENTITY);
            }
            if ($newName !== $file->getName()) {
                $existingFile = $fileRepository->findOneBy(['user' => $user, 'name' => $newName]);
                if ($existingFile) {
                    return $this->json(['message' => 'File name already exists'], Response::HTTP_CONFLICT);
                }
                $oldPath = $file->getPath();
                $newPath = $this->filePathGenerator->generate($newName);

                $file->setName($newName);
                $file->setPath($newPath);
            }
        }
        if (array_key_exists('dir', $data)) {
            $dir = null;
            if ($data['dir'] !== null) {
                $dir = $dirRepository->findOneBy(['user' => $user, 'id' => $data['dir']]);
                if (!$dir) {
                    return $this->json(['message' => 'Directory not found'], Response::HTTP_BAD_REQUEST);
                }
            }
            $file->setDir($dir);
        }

        $entityManager->flush();

        if (isset($oldPath, $newPath)) {
            $this->fileStorage->rename($oldPath, $newPath);
        }

        // Return the updated file's information.
        return $this->json([
            'id' => $file->getId(),
            'name' => $file->getName(),
            'dir' => $file->getDir() ? $file->getDir()->getId() : null,
        ]);
    }

    /**
     * Updates an existing directory.
     *
     * @param User|null $user The current user.
     * @param Request $request The request object.
     * @param Dir $dir The directory entity to update.
     * @param DirRepository $dirRepository The directory repository.
     * @param EntityManagerInterface $entityManager The entity manager.
     * @return Response The JSON response.
     */
    #[Route('/dir/{id}', name: 'dir_update', methods: ['PUT'])]
    public function updateDir(
        #[CurrentUser] User $user,
        Request $request,
        Dir $dir,
        DirRepository $dirRepository,
        EntityManagerInterface $entityManager
    ): Response {
        // Check if the user has access to the directory.
        if ($dir->getUser() !== $user) {
            return $this->json([
                'message' => 'You do not have access to this directory',
            ], Response::HTTP_FORBIDDEN);
        }

        // Decode the request content.
        $data = json_decode($request->getContent(), true);

        // Update directory properties if provided in the request.
        if (array_key_exists('name', $data)) {
            $newName = trim((string) ($data['name'] ?? ''));
            if ($newName === '' || mb_strlen($newName) > 255) {
                return $this->json(['message' => 'Invalid name: must be between 1 and 255 characters'], Response::HTTP_UNPROCESSABLE_ENTITY);
            }
            if ($newName !== $dir->getName()) {
                $existingDir = $dirRepository->findOneBy(['user' => $user, 'name' => $newName]);
                if ($existingDir) {
                    return $this->json(['message' => 'Directory name already exists'], Response::HTTP_CONFLICT);
                }
                $dir->setName($newName);
            }
        }
        if (array_key_exists('summary', $data)) {
            $summary = $data['summary'];
            if ($summary !== null && mb_strlen((string) $summary) > 2000) {
                return $this->json(['message' => 'Summary too long (max 2000 characters)'], Response::HTTP_UNPROCESSABLE_ENTITY);
            }
            $dir->setSummary($summary);
        }

        $entityManager->flush();

        // Return the updated directory's information.
        return $this->json([
            'id' => $dir->getId(),
            'name' => $dir->getName(),
            'summary' => $dir->getSummary(),
        ]);
    }

    /**
     * Deletes an existing file.
     *
     * @param User|null $user The current user.
     * @param File $file The file entity to delete.
     * @param EntityManagerInterface $entityManager The entity manager.
     * @return Response The JSON response.
     */
    #[Route('/file/{id}', name: 'file_delete', methods: ['DELETE'])]
    public function deleteFile(
        #[CurrentUser] User $user,
        File $file,
        EntityManagerInterface $entityManager
    ): Response {
        // Check if the user has access to the file.
        if ($file->getUser() !== $user) {
            return $this->json([
                'message' => 'You do not have access to this file',
            ], Response::HTTP_FORBIDDEN);
        }

        $entityManager->remove($file);
        $entityManager->flush();

        $this->fileStorage->delete($file->getPath());

        return $this->json(null, Response::HTTP_NO_CONTENT);
    }

    /**
     * Deletes an existing directory.
     *
     * @param User|null $user The current user.
     * @param Dir $dir The directory entity to delete.
     * @param EntityManagerInterface $entityManager The entity manager.
     * @return Response The JSON response.
     */
    #[Route('/dir/{id}', name: 'dir_delete', methods: ['DELETE'])]
    public function deleteDir(
        #[CurrentUser] User $user,
        Dir $dir,
        EntityManagerInterface $entityManager
    ): Response {
        // Check if the user has access to the directory.
        if ($dir->getUser() !== $user) {
            return $this->json([
                'message' => 'You do not have access to this directory',
            ], Response::HTTP_FORBIDDEN);
        }

        $pathsToDelete = [];
        foreach ($dir->getFiles() as $file) {
            $pathsToDelete[] = $file->getPath();
            $entityManager->remove($file);
        }

        $entityManager->remove($dir);
        $entityManager->flush();

        // Physical deletion happens after the DB commit — if flush threw, we never reach this.
        foreach ($pathsToDelete as $path) {
            $this->fileStorage->delete($path);
        }

        return $this->json(null, Response::HTTP_NO_CONTENT);
    }
}
