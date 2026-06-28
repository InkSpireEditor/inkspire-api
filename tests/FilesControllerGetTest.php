<?php

namespace App\Tests;

use App\Entity\User;
use App\Entity\Dir;
use App\Entity\File;
use Symfony\Component\HttpFoundation\Response;

class FilesControllerGetTest extends AuthenticatedWebTestCase
{
    public function test_01_treeEndpoint(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        // Create a loose file
        $looseFile = new File();
        $looseFile->setUser($user);
        $looseFile->setName('Loose File');
        $looseFile->setPath('/loose-file.md');
        $this->entityManager->persist($looseFile);

        // Create a directory
        $dir = new Dir();
        $dir->setUser($user);
        $dir->setName('Test Dir');
        $dir->setSummary('This is a test directory.');
        $this->entityManager->persist($dir);

        // Create a file inside the directory
        $fileInDir = new File();
        $fileInDir->setUser($user);
        $fileInDir->setName('File in Dir');
        $fileInDir->setPath('/test-dir/file-in-dir.md');
        $fileInDir->setDir($dir);
        $this->entityManager->persist($fileInDir);

        $this->entityManager->flush();

        $looseFileId = $looseFile->getId();
        $dirId = $dir->getId();
        $fileInDirId = $fileInDir->getId();

        $this->client->request('GET', '/api/tree');
        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_OK);
        $response = json_decode($this->client->getResponse()->getContent(), true);

        // Assert that the user is correct
        $this->assertEquals($this->email, $response['user']);

        // Assert that the loose file is present and has correct data
        $this->assertCount(1, $response['files']);
        $this->assertArrayHasKey($looseFileId, $response['files']);
        $this->assertEquals('Loose File', $response['files'][$looseFileId]['name']);

        // Assert that the directory is present and has correct data
        $this->assertCount(1, $response['dirs']);
        $this->assertArrayHasKey($dirId, $response['dirs']);
        $this->assertEquals('Test Dir', $response['dirs'][$dirId]['name']);
        $this->assertEquals('This is a test directory.', $response['dirs'][$dirId]['summary']);

        // Verify all entities in database
        $this->entityManager->clear();
        $fileRepository = $this->entityManager->getRepository(File::class);
        $dirRepository = $this->entityManager->getRepository(Dir::class);
        $userRepository = $this->entityManager->getRepository(User::class);

        $foundLooseFile = $fileRepository->find($looseFileId);
        $this->assertNotNull($foundLooseFile);
        $this->assertEquals('Loose File', $foundLooseFile->getName());
        $this->assertEquals('/loose-file.md', $foundLooseFile->getPath());
        $this->assertNull($foundLooseFile->getDir());
        $this->assertEquals($this->email, $foundLooseFile->getUser()->getEmail());

        $foundDir = $dirRepository->find($dirId);
        $this->assertNotNull($foundDir);
        $this->assertEquals('Test Dir', $foundDir->getName());
        $this->assertEquals('This is a test directory.', $foundDir->getSummary());
        $this->assertEquals($this->email, $foundDir->getUser()->getEmail());

        $foundFileInDir = $fileRepository->find($fileInDirId);
        $this->assertNotNull($foundFileInDir);
        $this->assertEquals('File in Dir', $foundFileInDir->getName());
        $this->assertEquals('/test-dir/file-in-dir.md', $foundFileInDir->getPath());
        $this->assertNotNull($foundFileInDir->getDir());
        $this->assertEquals($dirId, $foundFileInDir->getDir()->getId());
        $this->assertEquals($this->email, $foundFileInDir->getUser()->getEmail());
    }

    public function test_02_fileInfoEndpoint(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('Test File Info');
        $file->setPath('/test/path');
        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $fileId = $file->getId();

        $this->client->request('GET', '/api/file/' . $fileId);
        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_OK);
        $response = json_decode($this->client->getResponse()->getContent(), true);

        $this->assertEquals('Test File Info', $response['name']);

        // Verify file in database
        $this->entityManager->clear();
        $fileRepository = $this->entityManager->getRepository(File::class);
        $foundFile = $fileRepository->find($fileId);
        $this->assertNotNull($foundFile);
        $this->assertEquals('Test File Info', $foundFile->getName());
        $this->assertEquals('/test/path', $foundFile->getPath());
        $this->assertEquals($this->email, $foundFile->getUser()->getEmail());
    }

    public function test_03_dirInfoEndpoint(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        // Create a directory
        $dir = new Dir();
        $dir->setUser($user);
        $dir->setName('Another Test Dir');
        $dir->setSummary('Awesome summary');
        $this->entityManager->persist($dir);

        // Create a file inside the directory
        $fileInDir = new File();
        $fileInDir->setUser($user);
        $fileInDir->setName('Another File in Dir');
        $fileInDir->setPath('/test-dir/another-file-in-dir.md');
        $fileInDir->setDir($dir);
        $this->entityManager->persist($fileInDir);

        $this->entityManager->flush();

        $dirId = $dir->getId();
        $fileInDirId = $fileInDir->getId();

        $this->client->request('GET', '/api/dir/' . $dirId);
        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_OK);
        $response = json_decode($this->client->getResponse()->getContent(), true);

        $this->assertEquals('Another Test Dir', $response['name']);
        $this->assertEquals('Awesome summary', $response['summary']);

        // Assert that the file inside the directory is present and has correct data
        $this->assertCount(1, $response['files']);
        $this->assertArrayHasKey($fileInDirId, $response['files']);
        $this->assertEquals('Another File in Dir', $response['files'][$fileInDirId]['name']);

        // Verify directory and file in database
        $this->entityManager->clear();
        $dirRepository = $this->entityManager->getRepository(Dir::class);
        $fileRepository = $this->entityManager->getRepository(File::class);

        $foundDir = $dirRepository->find($dirId);
        $this->assertNotNull($foundDir);
        $this->assertEquals('Another Test Dir', $foundDir->getName());
        $this->assertEquals('Awesome summary', $foundDir->getSummary());
        $this->assertEquals($this->email, $foundDir->getUser()->getEmail());

        $foundFile = $fileRepository->find($fileInDirId);
        $this->assertNotNull($foundFile);
        $this->assertEquals('Another File in Dir', $foundFile->getName());
        $this->assertEquals('/test-dir/another-file-in-dir.md', $foundFile->getPath());
        $this->assertEquals($dirId, $foundFile->getDir()->getId());
        $this->assertEquals($this->email, $foundFile->getUser()->getEmail());
    }

    public function test_04_fileInfoForbidden(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user1 = $userRepository->findOneBy(['email' => $this->email]);
        $user2 = $this->createUser('user2@example.com', 'password');
        $this->entityManager->persist($user2);

        $file = new File();
        $file->setUser($user2);
        $file->setName('User2 File');
        $file->setPath('');
        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $fileId = $file->getId();

        $this->client->request('GET', '/api/file/' . $fileId);
        $this->assertResponseStatusCodeSame(Response::HTTP_FORBIDDEN);

        // Verify file still belongs to user2 in database
        $this->entityManager->clear();
        $fileRepository = $this->entityManager->getRepository(File::class);
        $foundFile = $fileRepository->find($fileId);
        $this->assertNotNull($foundFile);
        $this->assertEquals('User2 File', $foundFile->getName());
        $this->assertEquals('user2@example.com', $foundFile->getUser()->getEmail());
    }

    public function test_05_treeUnauthorized(): void
    {
        $this->deauthenticateClient();
        $this->client->request('GET', '/api/tree');
        $this->assertResponseStatusCodeSame(Response::HTTP_UNAUTHORIZED);
    }

    public function test_06_treeIsolation(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user1 = $userRepository->findOneBy(['email' => $this->email]);

        $user2 = $this->createUser('user2-isolation@example.com', 'password');
        $this->entityManager->persist($user2);

        $user2File = new File();
        $user2File->setUser($user2);
        $user2File->setName('User2 Secret File');
        $user2File->setPath('');
        $this->entityManager->persist($user2File);

        $user2Dir = new Dir();
        $user2Dir->setUser($user2);
        $user2Dir->setName('User2 Secret Dir');
        $this->entityManager->persist($user2Dir);

        $this->entityManager->flush();

        $user2FileId = $user2File->getId();
        $user2DirId = $user2Dir->getId();

        // Authenticated as user1; user2's resources must not appear in the tree
        $this->client->request('GET', '/api/tree');
        $this->assertResponseStatusCodeSame(Response::HTTP_OK);
        $response = json_decode($this->client->getResponse()->getContent(), true);

        $this->assertArrayNotHasKey($user2FileId, $response['files']);
        $this->assertArrayNotHasKey($user2DirId, $response['dirs']);
    }

    public function test_07_dirInfoForbidden(): void
    {
        $user2 = $this->createUser('user2-dir@example.com', 'password');
        $this->entityManager->persist($user2);

        $dir = new Dir();
        $dir->setUser($user2);
        $dir->setName('User2 Dir');
        $this->entityManager->persist($dir);
        $this->entityManager->flush();

        $dirId = $dir->getId();

        // Authenticated as user1; attempting to read user2's dir
        $this->client->request('GET', '/api/dir/' . $dirId);
        $this->assertResponseStatusCodeSame(Response::HTTP_FORBIDDEN);

        // Verify dir still belongs to user2
        $this->entityManager->clear();
        $dirRepository = $this->entityManager->getRepository(Dir::class);
        $foundDir = $dirRepository->find($dirId);
        $this->assertNotNull($foundDir);
        $this->assertEquals('user2-dir@example.com', $foundDir->getUser()->getEmail());
    }
}
